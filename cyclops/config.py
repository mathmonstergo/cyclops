import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Mapping

from dotenv import dotenv_values


class SettingsError(ValueError):
    pass


SETTINGS_ENV_FIELDS = {
    "DATABASE_URL": "database_url",
    "CHAT_BASE_URL": "chat_base_url",
    "CHAT_API_KEY": "chat_api_key",
    "CHAT_MODEL": "chat_model",
    "EMBEDDING_BASE_URL": "embedding_base_url",
    "EMBEDDING_API_KEY": "embedding_api_key",
    "EMBEDDING_MODEL": "embedding_model",
    "EMBEDDING_DIMENSIONS": "embedding_dimensions",
    "WECHAT_TOKEN_FILE": "wechat_token_file",
    "WECHAT_MESSAGE_CHUNK_SIZE": "wechat_message_chunk_size",
    "RAG_TOP_K": "rag_top_k",
    "RAG_MIN_SCORE": "rag_min_score",
    "UPLOAD_DIR": "upload_dir",
    "MINERU_API_MODE": "mineru_api_mode",
    "MINERU_API_TOKEN": "mineru_api_token",
    "MINERU_PARSE_TIMEOUT_SECONDS": "mineru_parse_timeout_seconds",
    "MINERU_USE_KB_PACKAGER": "mineru_use_kb_packager",
    "DOCUMENT_CHUNK_TOKEN_NUM": "document_chunk_token_num",
    "DOCUMENT_CHUNKER_TYPE": "document_chunker_type",
    "DOCUMENT_CHUNK_DELIMITER": "document_chunk_delimiter",
    "DOCUMENT_CHUNK_OVERLAP_PERCENT": "document_chunk_overlap_percent",
    "DOCUMENT_CHILDREN_DELIMITER": "document_children_delimiter",
    "DOCUMENT_TABLE_CONTEXT_SIZE": "document_table_context_size",
    "DOCUMENT_IMAGE_CONTEXT_SIZE": "document_image_context_size",
    "ADMIN_MAX_JSON_BYTES": "admin_max_json_bytes",
    "ADMIN_MAX_REQUEST_BYTES": "admin_max_request_bytes",
    "DB_POOL_MIN_SIZE": "db_pool_min_size",
    "DB_POOL_MAX_SIZE": "db_pool_max_size",
    "CHAT_TIMEOUT_SECONDS": "chat_timeout_seconds",
    "EMBEDDING_TIMEOUT_SECONDS": "embedding_timeout_seconds",
    "RERANK_TIMEOUT_SECONDS": "rerank_timeout_seconds",
    "ASSISTANT_MAX_CONCURRENT_STREAMS": "assistant_max_concurrent_streams",
    "IMPORT_PARSE_WORKER_POLL_INTERVAL_SECONDS": "import_parse_worker_poll_interval_seconds",
    "IMPORT_PARSE_WORKER_LEASE_SECONDS": "import_parse_worker_lease_seconds",
    "KG_EXTRACTION_WORKER_POLL_SECONDS": "kg_extraction_worker_poll_seconds",
    "KG_EXTRACTION_WORKER_LEASE_SECONDS": "kg_extraction_worker_lease_seconds",
    "RERANK_BASE_URL": "rerank_base_url",
    "RERANK_API_KEY": "rerank_api_key",
    "RERANK_MODEL": "rerank_model",
    "RERANK_INPUT_SIZE": "rerank_input_size",
}
DOCUMENT_CHUNKER_TYPES = {"manual", "naive", "qa", "table"}


@dataclass(frozen=True)
class Settings:
    embedding_schema_dimensions: ClassVar[int] = 1024

    database_url: str
    chat_base_url: str
    chat_api_key: str
    chat_model: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    embedding_dimensions: int = 1024
    wechat_token_file: Path = Path("/home/adam/.wxbot/token.json")
    wechat_message_chunk_size: int = 1800
    rag_top_k: int = 5
    rag_min_score: float = 0.35
    upload_dir: Path = Path("data/uploads")
    mineru_api_mode: str = "standard"
    mineru_api_token: str | None = None
    mineru_batch_file_url: str = "https://mineru.net/api/v4/file-urls/batch"
    mineru_batch_result_url_template: str = (
        "https://mineru.net/api/v4/extract-results/batch/{batch_id}"
    )
    mineru_parse_timeout_seconds: int = 600
    mineru_use_kb_packager: bool = True
    document_chunk_token_num: int = 512
    document_chunker_type: str = "naive"
    document_chunk_delimiter: str = "\n。；！？"
    document_chunk_overlap_percent: int = 0
    document_children_delimiter: str = ""
    document_table_context_size: int = 0
    document_image_context_size: int = 0
    admin_max_json_bytes: int = 10 * 1024 * 1024
    admin_max_request_bytes: int = 200 * 1024 * 1024
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    chat_timeout_seconds: float = 60.0
    embedding_timeout_seconds: float = 30.0
    rerank_timeout_seconds: float = 30.0
    assistant_max_concurrent_streams: int = 4
    import_parse_worker_poll_interval_seconds: float = 1.0
    import_parse_worker_lease_seconds: int = 60
    kg_extraction_worker_poll_seconds: float = 0.5
    kg_extraction_worker_lease_seconds: int = 180
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = ""
    rerank_input_size: int = 50

    @classmethod
    def load(
        cls,
        env_file: str | Path = ".env",
        *,
        local_settings_file: str | Path | None = None,
        tenant_id: str | None = None,
    ) -> "Settings":
        """读取启动环境和本地租户设置，并构造不可变配置对象。"""
        env_values = {
            key: value
            for key, value in dotenv_values(env_file).items()
            if value is not None
        }
        env_values.update(os.environ)
        settings_path = Path(
            local_settings_file or env_values.get("SETTINGS_FILE", "data/settings.local.json")
        )
        env_values.update(
            cls._local_settings_env(settings_path, tenant_id or env_values.get("TENANT_ID"))
        )
        return cls.from_env(env_values)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        """从环境变量构造配置，并校验会影响运行的关键约束。"""
        required_fields = {
            "DATABASE_URL": "database_url",
            "CHAT_BASE_URL": "chat_base_url",
            "CHAT_API_KEY": "chat_api_key",
            "CHAT_MODEL": "chat_model",
            "EMBEDDING_BASE_URL": "embedding_base_url",
            "EMBEDDING_API_KEY": "embedding_api_key",
            "EMBEDDING_MODEL": "embedding_model",
        }

        values: dict[str, object] = {}
        for env_name, field_name in required_fields.items():
            value = env.get(env_name, "").strip()
            if not value:
                raise SettingsError(f"Missing required environment variable: {env_name}")
            values[field_name] = value

        values["embedding_dimensions"] = cls._integer_env(env, "EMBEDDING_DIMENSIONS", 1024)
        # 当前数据库 schema 固定为 vector(1024)，这里提前拦截不匹配配置。
        if values["embedding_dimensions"] != cls.embedding_schema_dimensions:
            raise SettingsError(
                "EMBEDDING_DIMENSIONS must match database schema vector(1024)"
            )
        values["wechat_token_file"] = Path(
            env.get("WECHAT_TOKEN_FILE", "/home/adam/.wxbot/token.json")
        )
        values["wechat_message_chunk_size"] = cls._integer_env(
            env, "WECHAT_MESSAGE_CHUNK_SIZE", 1800
        )
        values["rag_top_k"] = cls._integer_env(env, "RAG_TOP_K", 5)
        values["rag_min_score"] = cls._float_env(env, "RAG_MIN_SCORE", 0.35)
        values["upload_dir"] = Path(env.get("UPLOAD_DIR", "data/uploads"))
        mineru_token = env.get("MINERU_API_TOKEN", "").strip() or None
        mineru_mode = env.get("MINERU_API_MODE", "standard").strip().lower()
        if mineru_mode not in {"standard"}:
            raise SettingsError("MINERU_API_MODE must be standard")
        values["mineru_api_mode"] = "standard"
        values["mineru_api_token"] = mineru_token
        values["mineru_batch_file_url"] = "https://mineru.net/api/v4/file-urls/batch"
        values["mineru_batch_result_url_template"] = (
            "https://mineru.net/api/v4/extract-results/batch/{batch_id}"
        )
        values["mineru_parse_timeout_seconds"] = cls._integer_env(
            env, "MINERU_PARSE_TIMEOUT_SECONDS", 600
        )
        values["mineru_use_kb_packager"] = cls._bool_env(env, "MINERU_USE_KB_PACKAGER", True)
        values["document_chunk_token_num"] = cls._integer_env(
            env,
            "DOCUMENT_CHUNK_TOKEN_NUM",
            512,
        )
        values["document_chunker_type"] = cls._document_chunker_env(env)
        values["document_chunk_delimiter"] = env.get(
            "DOCUMENT_CHUNK_DELIMITER",
            "\n。；！？",
        )
        values["document_chunk_overlap_percent"] = cls._integer_env(
            env,
            "DOCUMENT_CHUNK_OVERLAP_PERCENT",
            0,
        )
        values["document_children_delimiter"] = env.get("DOCUMENT_CHILDREN_DELIMITER", "")
        values["document_table_context_size"] = cls._integer_env(
            env,
            "DOCUMENT_TABLE_CONTEXT_SIZE",
            0,
        )
        values["document_image_context_size"] = cls._integer_env(
            env,
            "DOCUMENT_IMAGE_CONTEXT_SIZE",
            0,
        )
        values["admin_max_json_bytes"] = cls._integer_env(
            env,
            "ADMIN_MAX_JSON_BYTES",
            10 * 1024 * 1024,
        )
        values["admin_max_request_bytes"] = cls._integer_env(
            env,
            "ADMIN_MAX_REQUEST_BYTES",
            200 * 1024 * 1024,
        )
        values["db_pool_min_size"] = cls._integer_env(env, "DB_POOL_MIN_SIZE", 1)
        values["db_pool_max_size"] = cls._integer_env(env, "DB_POOL_MAX_SIZE", 10)
        if values["db_pool_min_size"] < 0:
            raise SettingsError("DB_POOL_MIN_SIZE must be non-negative")
        if values["db_pool_max_size"] < max(1, values["db_pool_min_size"]):
            raise SettingsError("DB_POOL_MAX_SIZE must be >= DB_POOL_MIN_SIZE and >= 1")
        values["chat_timeout_seconds"] = cls._float_env(env, "CHAT_TIMEOUT_SECONDS", 60.0)
        values["embedding_timeout_seconds"] = cls._float_env(
            env,
            "EMBEDDING_TIMEOUT_SECONDS",
            30.0,
        )
        values["rerank_timeout_seconds"] = cls._float_env(
            env,
            "RERANK_TIMEOUT_SECONDS",
            30.0,
        )
        values["assistant_max_concurrent_streams"] = cls._integer_env(
            env,
            "ASSISTANT_MAX_CONCURRENT_STREAMS",
            4,
        )
        if values["assistant_max_concurrent_streams"] < 1:
            raise SettingsError("ASSISTANT_MAX_CONCURRENT_STREAMS must be >= 1")
        values["import_parse_worker_poll_interval_seconds"] = cls._float_env(
            env,
            "IMPORT_PARSE_WORKER_POLL_INTERVAL_SECONDS",
            1.0,
        )
        if (
            not math.isfinite(values["import_parse_worker_poll_interval_seconds"])
            or values["import_parse_worker_poll_interval_seconds"] <= 0
        ):
            raise SettingsError(
                "IMPORT_PARSE_WORKER_POLL_INTERVAL_SECONDS must be a finite positive number"
            )
        values["import_parse_worker_lease_seconds"] = cls._integer_env(
            env,
            "IMPORT_PARSE_WORKER_LEASE_SECONDS",
            60,
        )
        if values["import_parse_worker_lease_seconds"] <= 0:
            raise SettingsError("IMPORT_PARSE_WORKER_LEASE_SECONDS must be positive")
        values["kg_extraction_worker_poll_seconds"] = cls._float_env(
            env,
            "KG_EXTRACTION_WORKER_POLL_SECONDS",
            0.5,
        )
        if (
            not math.isfinite(values["kg_extraction_worker_poll_seconds"])
            or values["kg_extraction_worker_poll_seconds"] <= 0
        ):
            raise SettingsError(
                "KG_EXTRACTION_WORKER_POLL_SECONDS must be a finite positive number"
            )
        values["kg_extraction_worker_lease_seconds"] = cls._integer_env(
            env,
            "KG_EXTRACTION_WORKER_LEASE_SECONDS",
            180,
        )
        if (
            values["kg_extraction_worker_lease_seconds"]
            <= values["chat_timeout_seconds"]
        ):
            raise SettingsError(
                "KG_EXTRACTION_WORKER_LEASE_SECONDS must be greater than "
                "CHAT_TIMEOUT_SECONDS"
            )
        values["rerank_base_url"] = env.get("RERANK_BASE_URL", "").strip()
        values["rerank_api_key"] = env.get("RERANK_API_KEY", "").strip()
        values["rerank_model"] = env.get("RERANK_MODEL", "").strip()
        values["rerank_input_size"] = cls._integer_env(env, "RERANK_INPUT_SIZE", 50)

        return cls(**values)

    @staticmethod
    def _integer_env(env: Mapping[str, str], name: str, default: int) -> int:
        value = env.get(name)
        if value is None or value == "":
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise SettingsError(f"{name} must be an integer") from exc

    @staticmethod
    def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
        """读取浮点环境变量；领域范围由调用方按具体配置继续校验。"""
        value = env.get(name)
        if value is None or value == "":
            return default
        try:
            return float(value)
        except ValueError as exc:
            raise SettingsError(f"{name} must be a float") from exc

    @staticmethod
    def _bool_env(env: Mapping[str, str], name: str, default: bool) -> bool:
        """读取布尔环境变量，支持常见 true/false 写法。"""
        value = env.get(name)
        if value is None or value == "":
            return default
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise SettingsError(f"{name} must be a boolean")

    @staticmethod
    def _document_chunker_env(env: Mapping[str, str]) -> str:
        """读取文档 chunker 类型，关键约束是只能使用 RAGFlow 对齐范围。"""
        value = env.get("DOCUMENT_CHUNKER_TYPE", "naive").strip().lower() or "naive"
        if value not in DOCUMENT_CHUNKER_TYPES:
            allowed = ", ".join(sorted(DOCUMENT_CHUNKER_TYPES))
            raise SettingsError(f"DOCUMENT_CHUNKER_TYPE must be one of: {allowed}")
        return value

    @staticmethod
    def _local_settings_env(path: Path, tenant_id: str | None = None) -> dict[str, str]:
        """读取本地租户设置文件，并转换成 Settings 可复用的环境键。"""
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SettingsError(f"Invalid local settings file: {path}") from exc
        tenants = payload.get("tenants")
        if not isinstance(tenants, dict):
            return {}
        active_tenant = tenant_id or payload.get("active_tenant_id") or "default"
        tenant_settings = tenants.get(active_tenant)
        if not isinstance(tenant_settings, dict):
            return {}
        result: dict[str, str] = {}
        for env_name, field_name in SETTINGS_ENV_FIELDS.items():
            if field_name not in tenant_settings:
                continue
            value = tenant_settings[field_name]
            if isinstance(value, bool):
                result[env_name] = "true" if value else "false"
            elif value is None:
                result[env_name] = ""
            else:
                result[env_name] = str(value)
        return result
