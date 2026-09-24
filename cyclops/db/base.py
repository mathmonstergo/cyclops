from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row


class BaseDatabase:
    """Database 主类基础：连接管理 + schema 初始化 + 行字典统一化。"""

    def __init__(
        self,
        database_url: str,
        *,
        pool: Any | None = None,
        pool_min_size: int = 0,
        pool_max_size: int = 0,
    ):
        """初始化数据库访问对象；关键约束是连接池参数只控制当前实例。"""
        self.database_url = database_url
        self.pool = pool
        self.pool_min_size = pool_min_size
        self.pool_max_size = pool_max_size

    def connect(self):
        """获取数据库连接上下文；关键约束是有连接池时复用池连接。"""
        if self.pool is not None:
            return self.pool.connection()
        if self.pool_max_size > 0:
            self.pool = ConnectionPool(
                conninfo=self.database_url,
                min_size=self.pool_min_size,
                max_size=self.pool_max_size,
                kwargs={"row_factory": dict_row},
            )
            return self.pool.connection()
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def close(self) -> None:
        """关闭数据库连接池；关键约束是只关闭本实例持有的池资源。"""
        if self.pool is None:
            return
        close = getattr(self.pool, "close", None)
        if callable(close):
            close()
        self.pool = None

    def init_schema(self, sql_path: str | Path = "sql/001_init.sql") -> None:
        sql = Path(sql_path).read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.execute(sql)

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        """psycopg dict_row 已返回 dict，这里只是统一对外类型。"""
        if isinstance(row, dict):
            return dict(row)
        return dict(row or {})
