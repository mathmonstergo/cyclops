from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cyclops.admin_server import (
    AdminApp,
    AdminConflictError,
    AdminNotFoundError,
    AdminPayloadTooLargeError,
    AdminValidationError,
    AiSuggestionError,
    ImportCandidateError,
    classify_error_response,
    ensure_loopback_or_explicit_opt_in,
    ensure_request_size,
    format_sse_event,
    jsonable,
    static_path,
)
from cyclops.config import Settings
from cyclops.import_parse_worker import ImportParseWorker
from cyclops.kg_extraction_worker import KgExtractionWorker


def create_app(
    *,
    settings: Settings | None = None,
    admin_app: Any | None = None,
    init_schema: bool = False,
    worker_factory: Callable[[Any], ImportParseWorker] | None = None,
    kg_worker_factory: Callable[[Any], KgExtractionWorker] | None = None,
) -> FastAPI:
    """创建 ASGI app，并在同一 lifespan 启停两个持久后台 worker。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """管理 AdminApp 生命周期；关键约束是测试可注入 fake app。"""
        if admin_app is not None:
            app.state.admin_app = admin_app
        else:
            loaded_settings = settings or Settings.load()
            app.state.admin_app = AdminApp(loaded_settings)
            if init_schema:
                app.state.admin_app.database().init_schema()
        factory = worker_factory or _create_import_parse_worker
        import_worker = factory(app.state.admin_app)
        kg_factory = kg_worker_factory or _create_kg_extraction_worker
        kg_worker = kg_factory(app.state.admin_app)
        app.state.import_parse_worker = import_worker
        app.state.kg_extraction_worker = kg_worker
        import_worker_task = asyncio.create_task(import_worker.run())
        kg_worker_task = asyncio.create_task(kg_worker.run())
        try:
            yield
        finally:
            import_worker.stop()
            kg_worker.stop()
            try:
                await import_worker_task
            finally:
                try:
                    await kg_worker_task
                finally:
                    _close_admin_resources(app.state.admin_app)

    app = FastAPI(title="Cyclops", lifespan=lifespan)
    if admin_app is not None:
        app.state.admin_app = admin_app
    _register_exception_handlers(app)
    _register_routes(app)
    _mount_static_dist(app)
    return app


def _create_import_parse_worker(admin_app: Any) -> ImportParseWorker:
    """按当前 Settings 构造唯一 worker，避免路由层复制生命周期配置。"""
    return ImportParseWorker(
        admin_app,
        poll_interval_seconds=(
            admin_app.settings.import_parse_worker_poll_interval_seconds
        ),
        lease_seconds=admin_app.settings.import_parse_worker_lease_seconds,
    )


def _create_kg_extraction_worker(admin_app: Any) -> KgExtractionWorker:
    """按当前 Settings 构造 KG worker，确保服务重启可恢复 active job。"""
    return KgExtractionWorker(
        admin_app,
        poll_interval_seconds=admin_app.settings.kg_extraction_worker_poll_seconds,
        lease_seconds=admin_app.settings.kg_extraction_worker_lease_seconds,
    )


def run_admin_asgi(settings: Settings, *, host: str, port: int) -> None:
    """启动 ASGI 管理后台；关键约束是保留原 admin 命令的 host/port 与 loopback 防护。"""
    ensure_loopback_or_explicit_opt_in(host, os.environ)
    app = create_app(settings=settings, init_schema=True)
    print(f"Cyclops admin: http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port)


def _register_exception_handlers(app: FastAPI) -> None:
    """注册统一异常响应；关键约束是只输出 AdminApp 分类后的当前错误结构。"""

    async def handle_exception(_request: Request, exc: Exception) -> JSONResponse:
        """统一处理未捕获异常；关键约束是 500 响应不泄漏内部细节。"""
        status, body = classify_error_response(exc)
        return JSONResponse(jsonable(body), status_code=status.value)

    for exc_type in (
        AdminValidationError,
        AdminNotFoundError,
        AdminConflictError,
        AdminPayloadTooLargeError,
        AiSuggestionError,
        ImportCandidateError,
    ):
        app.add_exception_handler(exc_type, handle_exception)
    app.add_exception_handler(Exception, handle_exception)


def _register_routes(app: FastAPI) -> None:
    """注册所有管理后台路由；关键约束是路径与当前 React 客户端契约一致。"""

    @app.get("/favicon.ico")
    def favicon() -> Response:
        """忽略 favicon 请求；关键约束是避免浏览器噪声进入错误日志。"""
        return Response(status_code=HTTPStatus.NO_CONTENT.value)

    @app.get("/")
    def index() -> FileResponse:
        """返回 React 入口；关键约束是根路径直接打开后台。"""
        return FileResponse(static_path("/"))

    @app.get("/api/settings")
    def get_settings(request: Request) -> Any:
        """读取设置快照；关键约束是复用 AdminApp 的脱敏逻辑。"""
        return _admin(request).settings_snapshot()

    @app.post("/api/settings")
    async def update_settings(request: Request) -> Any:
        """保存设置；关键约束是 JSON body 必须使用当前对象契约。"""
        return _admin(request).update_settings(await _read_json(request))

    @app.get("/api/retrieval/eval-cases")
    def list_retrieval_eval_cases(request: Request) -> Any:
        """列出检索评测用例；关键约束是查询参数保持多值列表结构。"""
        return _admin(request).list_retrieval_eval_cases(_query_params(request))

    @app.post("/api/retrieval/eval-cases")
    async def create_retrieval_eval_case(request: Request) -> Any:
        """创建检索评测用例；关键约束是仍由 AdminApp 做字段校验。"""
        return _admin(request).create_retrieval_eval_case(await _read_json(request))

    @app.post("/api/retrieval/eval-cases/{case_id}/run")
    async def run_retrieval_eval_case(case_id: str, request: Request) -> Any:
        """运行单条检索评测；关键约束是保留 case_id 路径参数。"""
        return _admin(request).run_retrieval_eval_case(case_id, await _read_json(request))

    @app.get("/api/retrieval/aliases")
    def list_retrieval_aliases(request: Request) -> Any:
        """列出检索别名；关键约束是使用当前前端调用的固定路径。"""
        return _admin(request).list_retrieval_aliases()

    @app.post("/api/retrieval/aliases")
    async def save_retrieval_alias(request: Request) -> Any:
        """保存检索别名；关键约束是别名校验仍在业务层。"""
        return _admin(request).save_retrieval_alias(await _read_json(request))

    @app.get("/api/kg/entities")
    def list_kg_entities(request: Request) -> Any:
        """列出 KG 实体；关键约束是状态和类型筛选均走当前审核契约。"""
        return _admin(request).list_kg_entities(_query_params(request))

    @app.get("/api/kg/relations")
    def list_kg_relations(request: Request) -> Any:
        """列出 KG 关系；关键约束是返回当前审核列表所需的端点与证据。"""
        return _admin(request).list_kg_relations(_query_params(request))

    @app.get("/api/kg/subgraph")
    def kg_subgraph(request: Request) -> Any:
        """读取 KG 子图；关键约束是只通过显式调试接口暴露。"""
        return _admin(request).kg_subgraph(_query_params(request))

    @app.get("/api/kg/extraction-jobs/{job_id}")
    def get_kg_extraction_job(job_id: str, request: Request) -> Any:
        """读取 KG 抽取任务状态，供前端轮询 completed 或 failed。"""
        return _admin(request).get_kg_extraction_job(job_id)

    @app.post("/api/faqs/{faq_id}/kg-extraction-jobs")
    async def queue_faq_kg_extraction_job(faq_id: str, request: Request) -> Any:
        """创建 FAQ queued 任务；关键约束是请求不会执行模型。"""
        return _admin(request).queue_faq_kg_extraction_job(
            faq_id,
            await _read_empty_json(request),
        )

    @app.get("/api/faqs/{faq_id}/kg-extraction-jobs/latest")
    def get_latest_faq_kg_extraction_job(faq_id: str, request: Request) -> Any:
        """读取 FAQ 最近任务；关键约束是只返回持久化公开 DTO。"""
        return _admin(request).get_latest_faq_kg_extraction_job(faq_id)

    @app.post("/api/import/files/{file_id}/kg-extraction-jobs")
    async def queue_document_kg_extraction_job(file_id: str, request: Request) -> Any:
        """创建整篇文档 queued 父任务；切片 Map 由持久 worker 推进。"""
        return _admin(request).queue_document_kg_extraction_job(
            file_id,
            await _read_empty_json(request),
        )

    @app.get("/api/import/files/{file_id}/kg-extraction-jobs/latest")
    def get_latest_document_kg_extraction_job(file_id: str, request: Request) -> Any:
        """读取文档最近父任务；关键约束是不接受单切片 ID。"""
        return _admin(request).get_latest_document_kg_extraction_job(file_id)

    @app.post("/api/kg/entities/{entity_id}/confirm")
    async def confirm_kg_entity(entity_id: str, request: Request) -> Any:
        """确认 KG 实体；关键约束是显式读取调用方审核的 revision。"""
        return _admin(request).confirm_kg_entity(entity_id, await _read_json(request))

    @app.post("/api/kg/entities/{entity_id}/status")
    async def set_kg_entity_status(entity_id: str, request: Request) -> Any:
        """更新 KG 实体状态；关键约束是只接受业务层允许的状态。"""
        return _admin(request).set_kg_entity_status(entity_id, await _read_json(request))

    @app.post("/api/kg/relations/{relation_id}/confirm")
    async def confirm_kg_relation(relation_id: str, request: Request) -> Any:
        """确认 KG 关系；关键约束是显式读取调用方审核的 revision。"""
        return _admin(request).confirm_kg_relation(relation_id, await _read_json(request))

    @app.post("/api/kg/relations/{relation_id}/status")
    async def set_kg_relation_status(relation_id: str, request: Request) -> Any:
        """更新 KG 关系状态；关键约束是同步投影由业务层完成。"""
        return _admin(request).set_kg_relation_status(relation_id, await _read_json(request))

    @app.get("/api/import/files")
    def list_import_files(request: Request) -> Any:
        """列出导入文件；关键约束是筛选参数使用当前列表契约。"""
        return _admin(request).list_import_files(_query_params(request))

    @app.post("/api/import/files")
    async def create_import_file(
        request: Request,
        file: UploadFile = File(...),
    ) -> Any:
        """上传导入文件；关键约束是读取前先做请求大小守门。"""
        length = int(request.headers.get("content-length", "0") or "0")
        ensure_request_size(length, _admin(request).settings.admin_max_request_bytes, "upload")
        content = await file.read()
        auto_parse = request.query_params.get("parse", "true").lower() != "false"
        return _admin(request).create_import_file(
            file.filename or "upload.bin",
            content,
            auto_parse=auto_parse,
        )

    @app.delete("/api/import/files/{file_id}")
    def delete_import_file(file_id: str, request: Request) -> Any:
        """删除导入文件；关键约束是文件与数据库清理由业务层完成。"""
        return _admin(request).delete_import_file(file_id)

    @app.get("/api/import/files/{file_id}")
    def get_import_file(file_id: str, request: Request) -> Any:
        """读取文件与最新解析任务；关键约束是 GET 不推进 provider。"""
        return _admin(request).get_import_file(file_id)

    @app.get("/api/import/files/{file_id}/download")
    def download_import_file(file_id: str, request: Request) -> FileResponse:
        """下载导入原件；关键约束是保留中文文件名编码。"""
        record, stored_path = _admin(request).get_import_file_for_download(file_id)
        filename = record.get("original_name") or stored_path.name
        return _download_response(stored_path, filename)

    @app.get("/api/import/files/{file_id}/assets/{asset_relpath:path}")
    def get_import_asset(file_id: str, asset_relpath: str, request: Request) -> FileResponse:
        """读取导入资产；关键约束是路径逃逸防护仍在业务层。"""
        _record, asset_path = _admin(request).get_import_asset(file_id, asset_relpath)
        return _file_response(asset_path)

    @app.get("/api/import/files/{file_id}/chunks")
    def list_import_chunks(file_id: str, request: Request) -> Any:
        """列出文件切片；关键约束是响应使用切片页面的当前结构。"""
        return _admin(request).list_import_chunks(file_id)

    @app.get("/api/import/parse-jobs/{job_id}")
    def get_import_parse_job(job_id: str, request: Request) -> Any:
        """只读解析任务状态；关键约束是任务推进完全属于 lifespan worker。"""
        return _admin(request).get_import_parse_job(job_id)

    @app.get("/api/import/files/{file_id}/candidates")
    def list_import_file_candidates(file_id: str, request: Request) -> Any:
        """列出文件候选 FAQ；关键约束是候选仍需人工审核。"""
        return _admin(request).list_import_file_candidates(file_id)

    @app.post("/api/import/files/{file_id}/parse-jobs")
    async def start_import_parse_job(file_id: str, request: Request) -> Any:
        """创建 queued 解析任务；关键约束是请求阶段不执行 provider。"""
        return _admin(request).start_import_parse_job(file_id, await _read_json(request))

    @app.post("/api/import/files/{file_id}/disabled")
    async def set_import_file_disabled(file_id: str, request: Request) -> Any:
        """设置文件禁用状态；关键约束是检索过滤由业务层保持一致。"""
        return _admin(request).set_import_file_disabled(file_id, await _read_json(request))

    @app.post("/api/import/files/{file_id}/generate-questions")
    async def generate_import_file_questions(file_id: str, request: Request) -> Any:
        """为文件生成候选问题；关键约束是生成结果不直接入正式 FAQ。"""
        return _admin(request).generate_import_file_questions(file_id, await _read_json(request))

    @app.post("/api/import/files/{file_id}/embed")
    def embed_import_file(file_id: str, request: Request) -> Any:
        """为文件切片生成向量；关键约束是只处理已解析内容。"""
        return _admin(request).embed_import_file(file_id)

    @app.get("/api/import/chunks/{chunk_id}/candidates")
    def list_import_candidates(chunk_id: str, request: Request) -> Any:
        """列出切片候选 FAQ；关键约束是候选审核只使用当前路由。"""
        return _admin(request).list_import_candidates(chunk_id)

    @app.post("/api/import/chunks/{chunk_id}/generate")
    def generate_import_candidates(chunk_id: str, request: Request) -> Any:
        """为单个切片生成候选 FAQ；关键约束是返回当前按钮所需的候选结果。"""
        return _admin(request).generate_import_candidates(chunk_id)

    @app.post("/api/import/chunks/{chunk_id}/disabled")
    async def set_import_chunk_disabled(chunk_id: str, request: Request) -> Any:
        """设置切片禁用状态；关键约束是禁用切片不参与检索。"""
        return _admin(request).set_import_chunk_disabled(chunk_id, await _read_json(request))

    @app.post("/api/import/chunks/{chunk_id}/embed")
    def embed_import_chunk(chunk_id: str, request: Request) -> Any:
        """为单个切片生成向量；关键约束是状态更新由业务层完成。"""
        return _admin(request).embed_import_chunk(chunk_id)

    @app.post("/api/import/chunks/{chunk_id}")
    async def update_import_chunk_text(chunk_id: str, request: Request) -> Any:
        """更新切片文本；关键约束是只改审核后的切片正文。"""
        return _admin(request).update_import_chunk_text(chunk_id, await _read_json(request))

    @app.post("/api/import/generation-jobs")
    async def create_import_generation_job(request: Request) -> Any:
        """创建批量生成任务；关键约束是事件流格式保持不变。"""
        return _admin(request).create_import_generation_job(await _read_json(request))

    @app.get("/api/import/generation-jobs/{job_id}/events")
    def iter_import_generation_events(job_id: str, request: Request) -> StreamingResponse:
        """输出导入生成事件；关键约束是使用现有 SSE formatter。"""
        return _sse_response(_admin(request).iter_import_generation_events(job_id))

    @app.post("/api/import/candidates/{candidate_id}/save")
    def save_import_candidate(candidate_id: str, request: Request) -> Any:
        """保存候选 FAQ；关键约束是正式保存仍走 needs_review 流程。"""
        return _admin(request).save_import_candidate(candidate_id)

    @app.post("/api/import/candidates/{candidate_id}/ignore")
    def ignore_import_candidate(candidate_id: str, request: Request) -> Any:
        """忽略候选 FAQ；关键约束是只影响候选审核状态。"""
        return _admin(request).ignore_import_candidate(candidate_id)

    @app.post("/api/import/candidates/{candidate_id}")
    async def update_import_candidate(candidate_id: str, request: Request) -> Any:
        """更新候选 FAQ；关键约束是编辑后仍等待人工保存。"""
        return _admin(request).update_import_candidate(candidate_id, await _read_json(request))

    @app.get("/api/faqs")
    def list_faqs(request: Request) -> Any:
        """列出 FAQ；关键约束是查询参数使用当前表格筛选结构。"""
        return _admin(request).list_faqs(_query_params(request))

    @app.post("/api/faqs")
    async def save_faq(request: Request) -> Any:
        """保存 FAQ；关键约束是正文和元数据校验仍在业务层。"""
        return _admin(request).save_faq(await _read_json(request))

    @app.post("/api/faqs/batch-status")
    async def batch_update_status(request: Request) -> Any:
        """批量更新 FAQ 状态；关键约束是状态集合由业务层限制。"""
        return _admin(request).batch_update_status(await _read_json(request))

    @app.post("/api/faqs/embed-pending")
    async def embed_pending(request: Request) -> Any:
        """生成待处理 FAQ 向量；关键约束是只处理业务层认定的 pending。"""
        return _admin(request).embed_pending(await _read_json(request))

    @app.get("/api/faqs/{faq_id}")
    def get_faq(faq_id: str, request: Request) -> Any:
        """读取单条 FAQ；关键约束是 faq_id 为当前唯一定位字段。"""
        return _admin(request).get_faq(faq_id)

    @app.post("/api/faqs/{faq_id}/embed")
    def embed_faq(faq_id: str, request: Request) -> Any:
        """为单条 FAQ 生成向量；关键约束是写入统一知识单元。"""
        return _admin(request).embed_faq(faq_id)

    @app.post("/api/ai/optimize")
    async def optimize(request: Request) -> Any:
        """执行 AI 辅助改写；关键约束是只返回建议不直接改事实。"""
        return _admin(request).optimize(await _read_json(request))

    @app.post("/api/assistant/chat-stream")
    async def iter_assistant_chat_events(request: Request) -> StreamingResponse:
        """输出问答 SSE；关键约束是保留 meta/step/delta/done/error 契约。"""
        payload = await _read_json(request)
        requester_type = request.headers.get("X-Requester-Type")
        requester_id = request.headers.get("X-Requester-Id")
        if requester_type and not payload.get("requester_type"):
            payload["requester_type"] = requester_type
        if requester_id and not payload.get("requester_id"):
            payload["requester_id"] = requester_id
        return _sse_response(_admin(request).iter_assistant_chat_events(payload))

    @app.post("/api/assistant/probe")
    async def probe_chat_provider(request: Request) -> Any:
        """探测 Chat 供应商；关键约束是后端可使用已保存密钥。"""
        return _admin(request).probe_chat_provider(await _read_json(request))

    @app.post("/api/assistant/models")
    async def list_chat_provider_models(request: Request) -> Any:
        """列出 Chat 模型；关键约束是不会把保存的密钥回传前端。"""
        return _admin(request).list_chat_provider_models(await _read_json(request))

    @app.get("/api/analytics/overview")
    def analytics_overview(request: Request) -> Any:
        """读取分析概览；关键约束是返回当前仪表盘所需的统计结构。"""
        return _admin(request).analytics_overview()

    @app.get("/api/analytics/top-queries")
    def list_top_queries(request: Request) -> Any:
        """列出高频问题；关键约束是分页参数使用当前列表契约。"""
        return _admin(request).list_top_queries(_query_params(request))

    @app.get("/api/analytics/zero-hit")
    def list_zero_hit_queries(request: Request) -> Any:
        """列出零命中问题；关键约束是用于补知识库而非自动写入。"""
        return _admin(request).list_zero_hit_queries(_query_params(request))

    @app.get("/api/analytics/low-score")
    def list_low_score_queries(request: Request) -> Any:
        """列出低分命中问题；关键约束是保留前端筛选参数。"""
        return _admin(request).list_low_score_queries(_query_params(request))

    @app.get("/api/analytics/top-chunks")
    def list_top_referenced_chunks(request: Request) -> Any:
        """列出高引用切片；关键约束是只读分析数据。"""
        return _admin(request).list_top_referenced_chunks(_query_params(request))

    @app.get("/api/analytics/hit-rate")
    def query_hit_rate_timeseries(request: Request) -> Any:
        """读取命中率时间序列；关键约束是时间范围使用当前查询契约。"""
        return _admin(request).query_hit_rate_timeseries(_query_params(request))

    @app.get("/api/analytics/cluster-summaries")
    def list_cluster_summaries(request: Request) -> Any:
        """列出聚类摘要；关键约束是返回已保存的分析结果。"""
        return _admin(request).list_cluster_summaries(_query_params(request))

    @app.post("/api/analytics/cluster-zero-hit")
    async def cluster_zero_hit_queries(request: Request) -> Any:
        """聚类零命中问题；关键约束是模型错误由业务层整理。"""
        return _admin(request).cluster_zero_hit_queries(await _read_json(request))


def _admin(request: Request) -> Any:
    """读取当前 AdminApp；关键约束是测试可注入 fake app。"""
    return request.app.state.admin_app


def _query_params(request: Request) -> dict[str, list[str]]:
    """把 FastAPI 查询参数转成 AdminApp 接受的多值字典。"""
    result: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        result.setdefault(key, []).append(value)
    return result


async def _read_json(request: Request) -> dict[str, Any]:
    """读取显式 JSON object；空 HTTP body 不是 `{}` 的替代请求形状。"""
    if request.headers.get("content-length") == "0":
        raise AdminValidationError("request body must be a JSON object")
    raw = await request.body()
    if not raw:
        raise AdminValidationError("request body must be a JSON object")
    ensure_request_size(len(raw), _admin(request).settings.admin_max_json_bytes, "json")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdminValidationError("request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise AdminValidationError("request body must be a JSON object")
    return payload


async def _read_empty_json(request: Request) -> dict[str, Any]:
    """读取资源级空对象命令；任何字段都属于已删除的客户端 dispatch。"""
    payload = await _read_json(request)
    if payload:
        raise AdminValidationError("request body must be an empty JSON object")
    return payload


def _sse_response(events: Iterable[dict[str, Any]]) -> StreamingResponse:
    """构造 SSE 响应；关键约束是 event/data 使用当前前端事件契约。"""

    def body() -> Iterable[str]:
        """逐条格式化 SSE；关键约束是异常也转成 error 事件。"""
        try:
            for event in events:
                yield format_sse_event(event)
        except Exception as exc:
            _status, payload = classify_error_response(exc)
            yield format_sse_event({"type": "error", "error": payload["error"]})

    return StreamingResponse(
        body(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


def _file_response(path: Path) -> FileResponse:
    """发送普通文件响应；关键约束是不存在文件映射为旧 404 结构。"""
    if not path.exists() or not path.is_file():
        raise AdminNotFoundError(str(path))
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])


def _download_response(path: Path, filename: str) -> FileResponse:
    """发送下载响应；关键约束是中文文件名使用 RFC 5987 编码。"""
    if not path.exists() or not path.is_file():
        raise AdminNotFoundError(str(path))
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    return FileResponse(
        path,
        filename=filename,
        media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        headers=headers,
    )


def _mount_static_dist(app: FastAPI) -> None:
    """挂载 Vite 产物目录；关键约束是保持 /static/dist/ 资源 URL 不变。"""
    dist_dir = Path(__file__).with_name("static") / "dist"
    app.mount(
        "/static/dist",
        StaticFiles(directory=dist_dir, check_dir=False),
        name="static-dist",
    )


def _close_admin_resources(admin_app: Any) -> None:
    """释放 ASGI app 持有资源；关键约束是只调用已初始化数据库的 close。"""
    db = getattr(admin_app, "db", None)
    close = getattr(db, "close", None)
    if callable(close):
        close()
