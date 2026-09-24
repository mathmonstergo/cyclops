import asyncio
import threading

from cyclops.import_parse_worker import ImportParseWorker


class _FakeParseJobDatabase:
    """按预设顺序返回解析任务，记录 worker 使用的 lease 配置。"""

    def __init__(self, claimed):
        """保存待领取任务；None 表示当前没有到期任务。"""
        self.claimed = list(claimed)
        self.lease_seconds = []
        self.renewals = []
        self.renewed = threading.Event()

    def claim_import_parse_job(self, *, lease_seconds):
        """模拟数据库原子 claim，不在 worker 内复制任务状态机。"""
        self.lease_seconds.append(lease_seconds)
        if not self.claimed:
            return None
        claimed = self.claimed.pop(0)
        if isinstance(claimed, Exception):
            raise claimed
        return claimed

    def renew_import_parse_job_lease(self, job_id, *, lease_token, lease_seconds):
        """模拟当前 token 续租，并唤醒等待中的长 provider 调用。"""
        self.renewals.append((job_id, lease_token, lease_seconds))
        self.renewed.set()
        return True


class _FakeParseJobAdmin:
    """记录 worker 交给业务层处理的持久解析任务。"""

    def __init__(self, claimed, *, fail_job_ids=(), wait_for_renewal=False):
        """注入数据库 claim 队列和需要模拟失败的任务 ID。"""
        self.db = _FakeParseJobDatabase(claimed)
        self.fail_job_ids = set(fail_job_ids)
        self.wait_for_renewal = wait_for_renewal
        self.processed = []

    def database(self):
        """返回唯一持久任务数据库入口。"""
        return self.db

    def process_import_parse_job(self, job):
        """记录处理顺序，并按任务 ID 模拟单项业务异常。"""
        self.processed.append(job["id"])
        if job["id"] in self.fail_job_ids:
            raise RuntimeError(f"failed: {job['id']}")
        if self.wait_for_renewal and not self.db.renewed.wait(timeout=1):
            raise AssertionError("long-running parse job was not renewed")


def _parse_job(job_id="parse_job_1", *, status="submitting"):
    """构造 worker 所需的最小持久任务事实。"""
    return {
        "id": job_id,
        "status": status,
        "lease_token": f"lease-{job_id}",
    }


def test_worker_continues_without_any_http_poll():
    """浏览器不请求 GET 时，worker 仍应连续领取并推进任务。"""
    admin = _FakeParseJobAdmin(
        [
            _parse_job(status="submitting"),
            _parse_job(status="polling"),
            None,
        ]
    )
    worker = ImportParseWorker(admin, poll_interval_seconds=0.01, lease_seconds=30)

    assert worker.run_available_once() is True
    assert worker.run_available_once() is True
    assert worker.run_available_once() is False
    assert admin.processed == ["parse_job_1", "parse_job_1"]
    assert admin.db.lease_seconds == [30, 30, 30]


def test_restarted_worker_processes_database_reclaimed_job():
    """服务重启后，新 worker 应处理数据库重新领取的过期 lease 任务。"""
    reclaimed = _parse_job("parse_job_reclaimed", status="finalizing")
    admin = _FakeParseJobAdmin([reclaimed])

    first_process_worker = ImportParseWorker(
        admin,
        poll_interval_seconds=0.01,
        lease_seconds=45,
    )

    assert first_process_worker.run_available_once() is True
    assert admin.processed == ["parse_job_reclaimed"]
    assert admin.db.lease_seconds == [45]


def test_single_job_exception_does_not_stop_following_jobs(caplog):
    """单项处理异常只等待 lease 恢复，不得终止 worker 后续领取。"""
    admin = _FakeParseJobAdmin(
        [_parse_job("parse_job_bad"), _parse_job("parse_job_good")],
        fail_job_ids={"parse_job_bad"},
    )
    worker = ImportParseWorker(admin, poll_interval_seconds=0.01, lease_seconds=30)

    assert worker.run_available_once() is True
    assert worker.run_available_once() is True
    assert admin.processed == ["parse_job_bad", "parse_job_good"]
    assert "parse_job_bad" in caplog.text


def test_long_running_job_renews_lease_before_provider_call_returns():
    """provider 调用超过心跳间隔时必须续租，避免第二个 worker 重领。"""
    job = _parse_job("parse_job_long")
    admin = _FakeParseJobAdmin([job], wait_for_renewal=True)
    worker = ImportParseWorker(admin, poll_interval_seconds=0.01, lease_seconds=30)
    worker._lease_renew_interval_seconds = 0.01

    assert worker.run_available_once() is True
    assert admin.db.renewals == [
        ("parse_job_long", "lease-parse_job_long", 30),
    ]


def test_worker_recovers_after_transient_claim_error(caplog):
    """数据库瞬时 claim 异常只延迟下一轮，不得永久终止 lifespan worker。"""
    admin = _FakeParseJobAdmin(
        [RuntimeError("database unavailable"), _parse_job("parse_job_recovered"), None]
    )
    worker = ImportParseWorker(admin, poll_interval_seconds=0.01, lease_seconds=30)

    async def exercise_worker():
        """等待第二次 claim 成功后停止，证明同一 run task 未退出。"""
        task = asyncio.create_task(worker.run())
        for _ in range(200):
            if admin.processed:
                break
            await asyncio.sleep(0.005)
        worker.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise_worker())

    assert admin.processed == ["parse_job_recovered"]
    assert "database unavailable" in caplog.text


def test_async_worker_stops_while_idle_without_pending_wait():
    """空闲循环收到 stop 后应立即退出，不遗留长时间 sleep。"""
    admin = _FakeParseJobAdmin([None, None])
    worker = ImportParseWorker(admin, poll_interval_seconds=30, lease_seconds=30)

    async def exercise_worker():
        """启动空闲 worker，等待首次 claim 后请求可控停止。"""
        task = asyncio.create_task(worker.run())
        for _ in range(100):
            if admin.db.lease_seconds:
                break
            await asyncio.sleep(0)
        worker.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise_worker())
    assert admin.db.lease_seconds


def test_worker_rejects_invalid_poll_and_lease_configuration():
    """worker 时间配置必须为正数，避免忙循环或永不恢复的 lease。"""
    admin = _FakeParseJobAdmin([])

    for poll_interval, lease_seconds in [(0, 30), (-1, 30), (0.1, 0), (0.1, -1)]:
        try:
            ImportParseWorker(
                admin,
                poll_interval_seconds=poll_interval,
                lease_seconds=lease_seconds,
            )
        except ValueError:
            continue
        raise AssertionError("invalid worker timing must raise ValueError")
