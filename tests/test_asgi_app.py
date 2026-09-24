import asyncio
from types import SimpleNamespace

from fastapi import Request
from fastapi.testclient import TestClient
import pytest

from cyclops.admin_server import AdminApp, AdminNotFoundError, AdminValidationError
from cyclops.asgi_app import create_app
from cyclops.db import KgReviewConflictError


class FakeAdminApp:
    """ASGI route adapter 测试用服务；关键约束是只验证 HTTP 适配层，不碰真实数据库。"""

    settings = SimpleNamespace(
        admin_max_json_bytes=1024,
        admin_max_request_bytes=1024,
        import_parse_worker_poll_interval_seconds=0.01,
        import_parse_worker_lease_seconds=30,
        kg_extraction_worker_poll_seconds=0.01,
        kg_extraction_worker_lease_seconds=180,
    )

    def __init__(self):
        """初始化 KG 和评测调用记录，供路由状态与请求体契约测试共享。"""
        self.kg_calls = []
        self.kg_jobs = {}
        self.eval_calls = []
        self.parse_calls = []

    def settings_snapshot(self):
        return {"app_name": "Cyclops", "chat_model": "deepseek-chat"}

    def update_settings(self, payload):
        return {"saved": payload}

    def list_faqs(self, params):
        assert params.get("status") == ["usable"]
        return {"items": [], "total": 0}

    def create_import_file(self, filename, content, *, auto_parse):
        assert filename == "manual.pdf"
        assert content == b"file-content"
        assert auto_parse is False
        return {"id": "file_1", "original_name": filename}

    def iter_assistant_chat_events(self, payload):
        assert payload["question"] == "怎么生成报告？"
        yield {"type": "meta", "flow_id": "basic_rag"}
        yield {
            "type": "done",
            "flow_id": "basic_rag",
            "question": payload["question"],
            "answer_draft": "请先检查报告任务状态。",
            "documents": [],
        }

    def queue_faq_kg_extraction_job(self, faq_id, payload):
        """模拟 FAQ 资源级排队，不接受客户端 source dispatch。"""
        self.kg_calls.append(("queue_faq", faq_id, payload))
        job = {
            "id": "kg_job_faq",
            "source_type": "faq",
            "source_id": faq_id,
            "phase": "queued",
        }
        self.kg_jobs[job["id"]] = dict(job)
        return dict(job)

    def queue_document_kg_extraction_job(self, file_id, payload):
        """模拟整篇文档资源级排队，切片只属于内部 manifest。"""
        self.kg_calls.append(("queue_document", file_id, payload))
        job = {
            "id": "kg_job_document",
            "source_type": "document",
            "source_id": file_id,
            "phase": "queued",
        }
        self.kg_jobs[job["id"]] = dict(job)
        return dict(job)

    def get_latest_faq_kg_extraction_job(self, faq_id):
        """返回 FAQ 最近任务，验证路径 owner 不从 payload 推断。"""
        self.kg_calls.append(("latest_faq", faq_id))
        return dict(self.kg_jobs["kg_job_faq"])

    def get_latest_document_kg_extraction_job(self, file_id):
        """返回文档最近任务，验证公开入口不接受 chunk ID。"""
        self.kg_calls.append(("latest_document", file_id))
        return dict(self.kg_jobs["kg_job_document"])

    def get_kg_extraction_job(self, job_id):
        """返回模拟任务状态，关键约束是按路径 ID 精确读取。"""
        self.kg_calls.append(("get", job_id))
        return dict(self.kg_jobs[job_id])

    def run_retrieval_eval_case(self, case_id, payload):
        """记录评测运行请求，验证 ASGI 只转发显式 JSON 对象。"""
        self.eval_calls.append((case_id, payload))
        return {"case_id": case_id, "payload": payload}

    def start_import_parse_job(self, file_id, payload):
        """模拟只创建 queued 解析任务，禁止请求阶段推进 provider。"""
        self.parse_calls.append(("enqueue", file_id, payload))
        return {"id": "parse_job_1", "file_id": file_id, "status": "queued"}

    def get_import_parse_job(self, job_id):
        """按任务 ID 返回持久解析快照，读取过程不得推进任务。"""
        self.parse_calls.append(("get_job", job_id))
        return {"id": job_id, "file_id": "file_1", "status": "polling"}

    def get_import_file(self, file_id):
        """按文件 ID 返回文件与最新任务，读取过程不得访问 provider。"""
        self.parse_calls.append(("get_file", file_id))
        return {
            "file": {"id": file_id, "status": "processing"},
            "parse_job": {"id": "parse_job_1", "status": "polling"},
        }


class FakeWorker:
    """记录 ASGI 后台 worker 启停顺序，run 只等待显式 stop。"""

    def __init__(self, calls, name):
        """保存 worker 名称；停止事件在所属事件循环内创建。"""
        self.calls = calls
        self.name = name
        self.started = False
        self.stopped = False
        self.finished = False
        self._stop_event = None

    async def run(self):
        """标记启动并等待 stop，验证 lifespan 会等待协程完整退出。"""
        self._stop_event = asyncio.Event()
        self.started = True
        self.calls.append(f"{self.name}-run")
        await self._stop_event.wait()
        self.finished = True
        self.calls.append(f"{self.name}-finished")

    def stop(self):
        """请求 worker 停止；关键约束是只设置事件而不伪造已退出。"""
        assert self._stop_event is not None
        self.stopped = True
        self.calls.append(f"{self.name}-stop")
        self._stop_event.set()


def test_asgi_app_serves_settings_and_faq_routes():
    """FastAPI app 应复用现有 /api 路径，避免前端迁移时改请求地址。"""
    client = TestClient(create_app(admin_app=FakeAdminApp()))

    assert client.get("/api/settings").json()["app_name"] == "Cyclops"
    assert client.get("/api/faqs?status=usable").json() == {"items": [], "total": 0}


def test_asgi_kg_subgraph_distinguishes_missing_isolated_and_failure():
    """子图 HTTP 必须分别返回 404、isolated 200 与脱敏 500。"""

    class SubgraphAdmin(FakeAdminApp):
        def kg_subgraph(self, params):
            """根据中心 ID 模拟不存在、故障或孤立子图。"""
            center_id = params["center_entity_id"][0]
            if center_id == "missing":
                raise AdminNotFoundError("KG center entity not found: missing")
            if center_id == "broken":
                raise RuntimeError("database unavailable")
            return {
                "state": "isolated",
                "center": {"id": center_id, "status": "usable"},
                "nodes": [{"id": center_id, "status": "usable"}],
                "edges": [],
            }

    client = TestClient(
        create_app(admin_app=SubgraphAdmin()),
        raise_server_exceptions=False,
    )

    missing = client.get("/api/kg/subgraph?center_entity_id=missing")
    isolated = client.get("/api/kg/subgraph?center_entity_id=kg_ent_1")
    broken = client.get("/api/kg/subgraph?center_entity_id=broken")

    assert missing.status_code == 404
    assert isolated.status_code == 200
    assert isolated.json()["state"] == "isolated"
    assert broken.status_code == 500
    assert broken.json() == {"error": "internal error"}


def test_asgi_app_exposes_migrated_admin_route_surface():
    """ASGI app 必须完整暴露管理端 API 路由面。"""
    app = create_app(admin_app=FakeAdminApp())
    route_pairs = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }
    expected_pairs = {
        ("GET", "/api/settings"),
        ("POST", "/api/settings"),
        ("GET", "/api/retrieval/eval-cases"),
        ("POST", "/api/retrieval/eval-cases"),
        ("POST", "/api/retrieval/eval-cases/{case_id}/run"),
        ("GET", "/api/retrieval/aliases"),
        ("POST", "/api/retrieval/aliases"),
        ("GET", "/api/kg/entities"),
        ("GET", "/api/kg/relations"),
        ("GET", "/api/kg/subgraph"),
        ("GET", "/api/kg/extraction-jobs/{job_id}"),
        ("POST", "/api/faqs/{faq_id}/kg-extraction-jobs"),
        ("GET", "/api/faqs/{faq_id}/kg-extraction-jobs/latest"),
        ("POST", "/api/import/files/{file_id}/kg-extraction-jobs"),
        ("GET", "/api/import/files/{file_id}/kg-extraction-jobs/latest"),
        ("POST", "/api/kg/entities/{entity_id}/confirm"),
        ("POST", "/api/kg/entities/{entity_id}/status"),
        ("POST", "/api/kg/relations/{relation_id}/confirm"),
        ("POST", "/api/kg/relations/{relation_id}/status"),
        ("GET", "/api/import/files"),
        ("POST", "/api/import/files"),
        ("DELETE", "/api/import/files/{file_id}"),
        ("GET", "/api/import/files/{file_id}"),
        ("GET", "/api/import/files/{file_id}/download"),
        ("GET", "/api/import/files/{file_id}/assets/{asset_relpath:path}"),
        ("GET", "/api/import/files/{file_id}/chunks"),
        ("GET", "/api/import/parse-jobs/{job_id}"),
        ("GET", "/api/import/files/{file_id}/candidates"),
        ("POST", "/api/import/files/{file_id}/parse-jobs"),
        ("POST", "/api/import/files/{file_id}/disabled"),
        ("POST", "/api/import/files/{file_id}/generate-questions"),
        ("POST", "/api/import/files/{file_id}/embed"),
        ("GET", "/api/import/chunks/{chunk_id}/candidates"),
        ("POST", "/api/import/chunks/{chunk_id}/generate"),
        ("POST", "/api/import/chunks/{chunk_id}/disabled"),
        ("POST", "/api/import/chunks/{chunk_id}/embed"),
        ("POST", "/api/import/chunks/{chunk_id}"),
        ("POST", "/api/import/generation-jobs"),
        ("GET", "/api/import/generation-jobs/{job_id}/events"),
        ("POST", "/api/import/candidates/{candidate_id}/save"),
        ("POST", "/api/import/candidates/{candidate_id}/ignore"),
        ("POST", "/api/import/candidates/{candidate_id}"),
        ("GET", "/api/faqs"),
        ("POST", "/api/faqs"),
        ("POST", "/api/faqs/batch-status"),
        ("POST", "/api/faqs/embed-pending"),
        ("GET", "/api/faqs/{faq_id}"),
        ("POST", "/api/faqs/{faq_id}/embed"),
        ("POST", "/api/ai/optimize"),
        ("POST", "/api/assistant/chat-stream"),
        ("POST", "/api/assistant/probe"),
        ("POST", "/api/assistant/models"),
        ("GET", "/api/analytics/overview"),
        ("GET", "/api/analytics/top-queries"),
        ("GET", "/api/analytics/zero-hit"),
        ("GET", "/api/analytics/low-score"),
        ("GET", "/api/analytics/top-chunks"),
        ("GET", "/api/analytics/hit-rate"),
        ("GET", "/api/analytics/cluster-summaries"),
        ("POST", "/api/analytics/cluster-zero-hit"),
    }

    assert expected_pairs <= route_pairs
    assert {
        ("POST", "/api/kg/extraction-jobs"),
        ("GET", "/api/import/files/{file_id}/parse-status"),
        ("POST", "/api/import/files/{file_id}/reparse"),
    }.isdisjoint(route_pairs)


def test_asgi_import_parse_routes_only_enqueue_or_read_persistent_state():
    """文档解析路由只允许入队和只读查询，不由 HTTP 请求推进 worker。"""
    fake = FakeAdminApp()
    client = TestClient(create_app(admin_app=fake))

    queued = client.post(
        "/api/import/files/file_1/parse-jobs",
        json={"chunker_type": "naive"},
    )
    job = client.get("/api/import/parse-jobs/parse_job_1")
    import_file = client.get("/api/import/files/file_1")

    assert queued.status_code == 200
    assert queued.json() == {"id": "parse_job_1", "file_id": "file_1", "status": "queued"}
    assert job.json() == {"id": "parse_job_1", "file_id": "file_1", "status": "polling"}
    assert import_file.json()["parse_job"] == {"id": "parse_job_1", "status": "polling"}
    assert fake.parse_calls == [
        ("enqueue", "file_1", {"chunker_type": "naive"}),
        ("get_job", "parse_job_1"),
        ("get_file", "file_1"),
    ]


def test_asgi_exposes_resource_kg_jobs_without_http_execution():
    """资源 POST 只持久化 queued，latest/job GET 都不得触发 worker step。"""
    fake = FakeAdminApp()
    client = TestClient(create_app(admin_app=fake))

    faq = client.post(
        "/api/faqs/faq_1/kg-extraction-jobs",
        json={},
    )
    latest_faq = client.get("/api/faqs/faq_1/kg-extraction-jobs/latest")
    document = client.post(
        "/api/import/files/imp_1/kg-extraction-jobs",
        json={},
    )
    latest_document = client.get(
        "/api/import/files/imp_1/kg-extraction-jobs/latest"
    )
    by_id = client.get("/api/kg/extraction-jobs/kg_job_faq")

    assert faq.status_code == 200
    assert faq.json()["phase"] == "queued"
    assert latest_faq.json() == faq.json()
    assert document.status_code == 200
    assert document.json()["phase"] == "queued"
    assert latest_document.json() == document.json()
    assert by_id.json() == faq.json()
    assert fake.kg_calls == [
        ("queue_faq", "faq_1", {}),
        ("latest_faq", "faq_1"),
        ("queue_document", "imp_1", {}),
        ("latest_document", "imp_1"),
        ("get", "kg_job_faq"),
    ]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/faqs/faq_1/kg-extraction-jobs", b""),
        ("/api/faqs/faq_1/kg-extraction-jobs", b"null"),
        ("/api/faqs/faq_1/kg-extraction-jobs", b"[]"),
        (
            "/api/faqs/faq_1/kg-extraction-jobs",
            b'{"source_type":"faq"}',
        ),
        ("/api/import/files/imp_1/kg-extraction-jobs", b""),
        ("/api/import/files/imp_1/kg-extraction-jobs", b"null"),
        ("/api/import/files/imp_1/kg-extraction-jobs", b"[]"),
        (
            "/api/import/files/imp_1/kg-extraction-jobs",
            b'{"chunk_id":"chunk_1"}',
        ),
    ],
)
def test_asgi_resource_kg_jobs_reject_nonempty_or_nonobject_body(path, body):
    """资源级 KG POST 只接受显式空对象，不保留 source/chunk payload。"""
    fake = FakeAdminApp()
    client = TestClient(create_app(admin_app=fake))

    response = client.post(
        path,
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert fake.kg_calls == []


def test_asgi_eval_run_requires_explicit_json_object_body():
    """评测 baseline 必须显式发送 `{}`，空 HTTP body 不是第二种兼容请求形状。"""
    fake = FakeAdminApp()
    app = create_app(admin_app=fake)
    route = next(
        item
        for item in app.routes
        if item.path == "/api/retrieval/eval-cases/{case_id}/run" and "POST" in item.methods
    )

    async def empty_receive():
        """向路由提供空请求体，验证不会被静默解释成 baseline。"""
        return {"type": "http.request", "body": b"", "more_body": False}

    baseline_body = b"{}"

    async def baseline_receive():
        """向路由提供显式空对象，验证唯一 baseline wire 形状。"""
        return {"type": "http.request", "body": baseline_body, "more_body": False}

    def request_for(body: bytes, receive):
        """构造带精确 content-length 的 ASGI Request，绕开外部 TestClient transport。"""
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/retrieval/eval-cases/eval_1/run",
                "raw_path": b"/api/retrieval/eval-cases/eval_1/run",
                "query_string": b"",
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
                "client": ("test", 50000),
                "server": ("test", 80),
                "app": app,
            },
            receive,
        )

    empty_request = request_for(b"", empty_receive)
    baseline_request = request_for(baseline_body, baseline_receive)

    with pytest.raises(AdminValidationError, match="JSON object"):
        asyncio.run(route.endpoint("eval_1", empty_request))
    baseline = asyncio.run(route.endpoint("eval_1", baseline_request))

    assert baseline == {"case_id": "eval_1", "payload": {}}
    assert fake.eval_calls == [("eval_1", {})]


def test_asgi_app_maps_kg_confirm_validation_missing_and_revision_conflict():
    """KG 确认分别返回校验 400、不存在 404 和审核版本冲突 409。"""

    class FakeDatabase:
        def confirm_kg_entity(self, entity_id, *, expected_revision):
            """按实体 ID 模拟确认门禁、不存在或 revision 冲突。"""
            assert expected_revision == 1
            if entity_id == "kg_ent_invalid":
                raise ValueError("KG entity requires at least one valid evidence")
            if entity_id == "kg_ent_stale":
                raise KgReviewConflictError("KG entity review revision changed")
            raise KeyError(f"KG entity not found: {entity_id}")

    admin = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            admin_max_json_bytes=1024,
        ),
        db=FakeDatabase(),
    )
    client = TestClient(create_app(admin_app=admin))

    invalid = client.post(
        "/api/kg/entities/kg_ent_invalid/confirm",
        json={"expected_revision": 1},
    )
    missing = client.post(
        "/api/kg/entities/kg_ent_missing/confirm",
        json={"expected_revision": 1},
    )
    stale = client.post(
        "/api/kg/entities/kg_ent_stale/confirm",
        json={"expected_revision": 1},
    )

    assert invalid.status_code == 400
    assert invalid.json() == {"error": "KG entity requires at least one valid evidence"}
    assert missing.status_code == 404
    assert "KG entity not found" in missing.json()["error"]
    assert stale.status_code == 409
    assert stale.json() == {"error": "KG entity review revision changed"}


def test_asgi_app_rejects_empty_kg_confirm_body_before_admin_call():
    """KG confirm 不能把空 HTTP body 当成当前 revision，必须在业务调用前拒绝。"""

    class ConfirmAdmin(FakeAdminApp):
        def confirm_kg_entity(self, entity_id, payload):
            """空 body 不得进入 AdminApp 确认方法。"""
            raise AssertionError(f"empty confirm reached AdminApp: {entity_id} {payload}")

    client = TestClient(create_app(admin_app=ConfirmAdmin()))

    response = client.post("/api/kg/entities/kg_ent_1/confirm")

    assert response.status_code == 400
    assert response.json() == {"error": "request body must be a JSON object"}


def test_asgi_app_accepts_import_upload_with_parse_flag():
    """上传路由应使用 FastAPI UploadFile，同时保留 parse=false 语义。"""
    client = TestClient(create_app(admin_app=FakeAdminApp()))

    response = client.post(
        "/api/import/files?parse=false",
        files={"file": ("manual.pdf", b"file-content", "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json() == {"id": "file_1", "original_name": "manual.pdf"}


def test_asgi_app_serves_static_root():
    """根路径应返回 Vite 构建入口，保持管理后台无需 admin.html。"""
    client = TestClient(create_app(admin_app=FakeAdminApp()))

    response = client.get("/")

    assert response.status_code == 200
    assert "Cyclops" in response.text
    assert "/static/dist/assets/" in response.text


def test_asgi_app_serves_download_and_import_assets(tmp_path):
    """下载和资产路由应保留文件响应语义与中文文件名编码。"""
    stored = tmp_path / "stored.bin"
    stored.write_bytes(b"download-body")
    asset = tmp_path / "asset.txt"
    asset.write_text("asset-body", encoding="utf-8")

    class FileAdminApp(FakeAdminApp):
        def get_import_file_for_download(self, file_id):
            assert file_id == "file_1"
            return {"original_name": "资料.pdf"}, stored

        def get_import_asset(self, file_id, asset_relpath):
            assert file_id == "file_1"
            assert asset_relpath == "images/a.txt"
            return {"id": file_id}, asset

    client = TestClient(create_app(admin_app=FileAdminApp()))

    download = client.get("/api/import/files/file_1/download")
    asset_response = client.get("/api/import/files/file_1/assets/images/a.txt")

    assert download.status_code == 200
    assert download.content == b"download-body"
    assert "filename*=UTF-8''%E8%B5%84%E6%96%99.pdf" in download.headers["content-disposition"]
    assert asset_response.status_code == 200
    assert asset_response.text == "asset-body"


def test_asgi_app_streams_assistant_events_as_sse():
    """Assistant route 必须保持现有 SSE event/data 格式，前端无需改解析器。"""
    client = TestClient(create_app(admin_app=FakeAdminApp()))

    response = client.post("/api/assistant/chat-stream", json={"question": "怎么生成报告？"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: meta" in response.text
    assert "event: done" in response.text
    assert '"answer_draft": "请先检查报告任务状态。"' in response.text


def test_asgi_app_rejects_invalid_json_as_validation_error():
    """JSON 解析失败应返回 400，避免非法请求被脱敏成泛化 500。"""
    client = TestClient(create_app(admin_app=FakeAdminApp()))

    response = client.post(
        "/api/settings",
        content=b"{",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "request body must be valid JSON"}


def test_asgi_app_lifespan_closes_database_pool():
    """两个持久 worker 都退出后才允许关闭已初始化的数据库资源。"""
    calls = []

    class FakeDatabase:
        def close(self):
            calls.append("close")

    fake_app = FakeAdminApp()
    fake_app.db = FakeDatabase()
    import_worker = FakeWorker(calls, "import")
    kg_worker = FakeWorker(calls, "kg")

    def worker_factory(admin_app):
        """返回固定 fake worker，并验证 factory 收到当前 AdminApp。"""
        assert admin_app is fake_app
        return import_worker

    def kg_worker_factory(admin_app):
        """返回独立 KG worker，验证其生命周期也受 app 管理。"""
        assert admin_app is fake_app
        return kg_worker

    with TestClient(
        create_app(
            admin_app=fake_app,
            worker_factory=worker_factory,
            kg_worker_factory=kg_worker_factory,
        )
    ):
        assert import_worker.started is True
        assert kg_worker.started is True

    assert import_worker.stopped is True
    assert import_worker.finished is True
    assert kg_worker.stopped is True
    assert kg_worker.finished is True
    assert calls[-1] == "close"
    assert calls.index("import-finished") < calls.index("close")
    assert calls.index("kg-finished") < calls.index("close")


def test_lifespan_starts_and_stops_persistent_workers():
    """服务启动两个 worker，关闭时先等待二者再释放数据库。"""
    calls = []
    fake_app = FakeAdminApp()
    import_worker = FakeWorker(calls, "import")
    kg_worker = FakeWorker(calls, "kg")

    def worker_factory(admin_app):
        """记录唯一 worker 构造，防止 lifespan 重复启动后台任务。"""
        assert admin_app is fake_app
        calls.append("import-factory")
        return import_worker

    def kg_worker_factory(admin_app):
        """记录 KG worker 构造，防止 lifespan 重复启动。"""
        assert admin_app is fake_app
        calls.append("kg-factory")
        return kg_worker

    with TestClient(
        create_app(
            admin_app=fake_app,
            worker_factory=worker_factory,
            kg_worker_factory=kg_worker_factory,
        )
    ):
        assert import_worker.started is True
        assert kg_worker.started is True
        assert import_worker.stopped is False
        assert kg_worker.stopped is False

    assert import_worker.stopped is True
    assert import_worker.finished is True
    assert kg_worker.stopped is True
    assert kg_worker.finished is True
    assert calls[:4] == ["import-factory", "kg-factory", "import-run", "kg-run"]


def test_admin_server_exposes_no_legacy_http_server_entrypoint():
    """管理服务只保留 ASGI 入口，不存在 make_handler 或 ThreadingHTTPServer 双路径。"""
    import cyclops.admin_server as admin_server

    assert not hasattr(admin_server, "make_handler")
    assert not hasattr(admin_server, "run_admin_server")
