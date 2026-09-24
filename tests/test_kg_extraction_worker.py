import asyncio
import threading

import pytest

from cyclops.kg_extraction_worker import KgExtractionWorker


class _FakeKgJobDatabase:
    """按预设顺序返回 KG 任务，并记录 worker 的 claim 行为。"""

    def __init__(self, claimed):
        """保存待领取结果；异常对象表示一次瞬时 claim 失败。"""
        self.claimed = list(claimed)
        self.claim_calls = 0
        self.lease_seconds = []
        self.claim_attempted = threading.Event()

    def claim_kg_extraction_job(self, *, lease_seconds):
        """模拟数据库原子 claim，不在 worker 内复制 KG 状态机。"""
        self.claim_calls += 1
        self.lease_seconds.append(lease_seconds)
        self.claim_attempted.set()
        if not self.claimed:
            return None
        claimed = self.claimed.pop(0)
        if isinstance(claimed, Exception):
            raise claimed
        return claimed


class _FakeKgAdmin:
    """模拟 Admin 单步业务边界，并区分已持久化与未持久化失败。"""

    def __init__(
        self,
        claimed=(),
        *,
        database=None,
        failed_job_ids=(),
        unpersisted_job_ids=(),
    ):
        """注入 claim 队列以及两类失败任务，供 worker 契约测试使用。"""
        self.db = database or _FakeKgJobDatabase(claimed)
        self.failed_job_ids = set(failed_job_ids)
        self.unpersisted_job_ids = set(unpersisted_job_ids)
        self.processed = []
        self.failed = []
        self.map_chat_calls = []

    def database(self):
        """返回唯一 KG 持久任务数据库入口。"""
        return self.db

    def process_kg_extraction_job_step(self, job):
        """每次只处理一个已 claim 阶段；普通模型失败在此持久化。"""
        self.processed.append((job["id"], job["phase"]))
        if job["id"] in self.unpersisted_job_ids:
            raise RuntimeError(f"failure persistence unavailable: {job['id']}")
        if job["id"] in self.failed_job_ids:
            error = f"model failed: {job['id']}"
            self.failed.append((job["id"], job["lease_token"], error))
            return {**job, "phase": "failed", "error": error}
        if job["phase"] == "mapping":
            self.map_chat_calls.append(job["id"])
        return dict(job)


class _LeaseAwareKgDatabase:
    """用可控时钟模拟进程崩溃后 lease 到期重领。"""

    def __init__(self):
        """创建一个尚未领取的 mapping job 和单调测试时钟。"""
        self.now = 0
        self.lease_token = None
        self.lease_expires_at = None
        self.claim_count = 0

    def claim_kg_extraction_job(self, *, lease_seconds):
        """只在无有效 lease 时返回同一 job，并签发新的 fencing token。"""
        if self.lease_token is not None and self.lease_expires_at > self.now:
            return None
        self.claim_count += 1
        self.lease_token = f"lease-{self.claim_count}"
        self.lease_expires_at = self.now + lease_seconds
        return _kg_job("kg_job_reclaimed", lease_token=self.lease_token)


def _kg_job(job_id="kg_job_1", *, phase="mapping", lease_token=None):
    """构造 worker 所需的最小已 claim KG job。"""
    return {
        "id": job_id,
        "source_type": "document",
        "phase": phase,
        "lease_token": lease_token or f"lease-{job_id}-{phase}",
    }


def test_worker_advances_document_without_any_http_get():
    """浏览器不轮询时，worker 仍依次推进 Map、Resolve、Reduce。"""
    admin = _FakeKgAdmin(
        [
            _kg_job(phase="mapping"),
            _kg_job(phase="resolving"),
            _kg_job(phase="reducing"),
            None,
        ]
    )
    worker = KgExtractionWorker(admin, poll_interval_seconds=0.01, lease_seconds=180)

    assert worker.run_available_once() is True
    assert worker.run_available_once() is True
    assert worker.run_available_once() is True
    assert worker.run_available_once() is False
    assert admin.processed == [
        ("kg_job_1", "mapping"),
        ("kg_job_1", "resolving"),
        ("kg_job_1", "reducing"),
    ]
    assert admin.db.lease_seconds == [180, 180, 180, 180]


def test_worker_reclaims_expired_lease_after_restart():
    """worker A 崩溃后，worker B 只能在 lease 过期后重领同一阶段。"""
    database = _LeaseAwareKgDatabase()
    crashed_claim = database.claim_kg_extraction_job(lease_seconds=10)
    admin = _FakeKgAdmin(database=database)
    worker = KgExtractionWorker(admin, poll_interval_seconds=0.01, lease_seconds=10)

    assert crashed_claim["lease_token"] == "lease-1"
    assert worker.run_available_once() is False

    database.now = 11
    assert worker.run_available_once() is True
    assert admin.processed == [("kg_job_reclaimed", "mapping")]
    assert database.lease_token == "lease-2"


def test_persisted_job_failure_does_not_stop_following_job(caplog):
    """Admin 已按 lease 标 failed 后，worker 记录错误并继续后续任务。"""
    admin = _FakeKgAdmin(
        [_kg_job("kg_job_bad"), _kg_job("kg_job_good")],
        failed_job_ids={"kg_job_bad"},
    )
    worker = KgExtractionWorker(admin, poll_interval_seconds=0.01, lease_seconds=180)

    assert worker.run_available_once() is True
    assert worker.run_available_once() is True
    assert admin.failed == [
        ("kg_job_bad", "lease-kg_job_bad-mapping", "model failed: kg_job_bad")
    ]
    assert admin.processed == [
        ("kg_job_bad", "mapping"),
        ("kg_job_good", "mapping"),
    ]
    assert "kg_job_bad" in caplog.text


def test_worker_does_not_swallow_unpersisted_job_failure(caplog):
    """Admin 未能持久化失败时，worker 必须让异常终止当前执行链。"""
    admin = _FakeKgAdmin(
        [_kg_job("kg_job_unpersisted")],
        unpersisted_job_ids={"kg_job_unpersisted"},
    )
    worker = KgExtractionWorker(admin, poll_interval_seconds=0.01, lease_seconds=180)

    with pytest.raises(RuntimeError, match="failure persistence unavailable"):
        worker.run_available_once()

    assert "kg_job_unpersisted" in caplog.text


def test_worker_recovers_after_transient_claim_error(caplog):
    """数据库瞬时 claim 异常只延迟下一轮，不终止 lifespan worker。"""
    admin = _FakeKgAdmin(
        [RuntimeError("database unavailable"), _kg_job("kg_job_recovered"), None]
    )
    worker = KgExtractionWorker(admin, poll_interval_seconds=0.01, lease_seconds=180)

    async def exercise_worker():
        """等待第二次 claim 成功后停止，证明同一 run task 仍在运行。"""
        task = asyncio.create_task(worker.run())
        for _ in range(200):
            if admin.processed:
                break
            await asyncio.sleep(0.005)
        worker.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise_worker())

    assert admin.processed == [("kg_job_recovered", "mapping")]
    assert "database unavailable" in caplog.text


def test_empty_queue_waits_instead_of_busy_loop():
    """空队列必须等待 stop event timeout，不能连续占用 CPU claim。"""
    admin = _FakeKgAdmin([None, None])
    worker = KgExtractionWorker(admin, poll_interval_seconds=0.5, lease_seconds=180)

    async def exercise_worker():
        """观察首轮 claim 后的短窗口，确认下一轮仍处于事件等待。"""
        task = asyncio.create_task(worker.run())
        for _ in range(100):
            if admin.db.claim_attempted.is_set():
                break
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.03)
        assert admin.db.claim_calls == 1
        worker.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise_worker())


def test_async_worker_stops_immediately_while_idle():
    """空闲等待收到 stop 后应立即退出，不等待完整轮询周期。"""
    admin = _FakeKgAdmin([None])
    worker = KgExtractionWorker(admin, poll_interval_seconds=30, lease_seconds=180)

    async def exercise_worker():
        """启动空闲 worker，确认首轮 claim 后请求可控停止。"""
        task = asyncio.create_task(worker.run())
        for _ in range(100):
            if admin.db.claim_attempted.is_set():
                break
            await asyncio.sleep(0.001)
        worker.stop()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise_worker())
    assert admin.db.claim_calls == 1


def test_already_mapped_item_is_not_sent_to_chat_again():
    """数据库未再次 claim 已 mapped item 时，worker 不会重复调用 Map Chat。"""
    admin = _FakeKgAdmin([_kg_job("kg_job_mapped"), None])
    worker = KgExtractionWorker(admin, poll_interval_seconds=0.01, lease_seconds=180)

    assert worker.run_available_once() is True
    assert worker.run_available_once() is False
    assert admin.map_chat_calls == ["kg_job_mapped"]


def test_worker_rejects_invalid_poll_and_lease_configuration():
    """轮询与 lease 必须是正数，避免忙循环或无法恢复的任务。"""
    admin = _FakeKgAdmin()

    for poll_interval, lease_seconds in [
        (0, 180),
        (-1, 180),
        (True, 180),
        (0.1, 0),
        (0.1, -1),
        (0.1, True),
        (0.1, 1.5),
    ]:
        with pytest.raises((TypeError, ValueError)):
            KgExtractionWorker(
                admin,
                poll_interval_seconds=poll_interval,
                lease_seconds=lease_seconds,
            )
