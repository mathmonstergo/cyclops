from __future__ import annotations

import asyncio
import logging
from typing import Any


logger = logging.getLogger(__name__)


class KgExtractionWorker:
    """在 ASGI 生命周期内逐步推进持久 KG job，关闭时可控停止。"""

    def __init__(
        self,
        admin_app: Any,
        *,
        poll_interval_seconds: float,
        lease_seconds: int,
    ) -> None:
        """绑定唯一 AdminApp；轮询间隔和 lease 必须是明确正数。"""
        if (
            not isinstance(poll_interval_seconds, int | float)
            or isinstance(poll_interval_seconds, bool)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be positive")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        self.admin_app = admin_app
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.lease_seconds = lease_seconds
        self._stop_event = asyncio.Event()

    def run_available_once(self) -> bool:
        """claim 一个 job 并交给 Admin 单步推进，不保存第二份任务状态。"""
        try:
            job = self.admin_app.database().claim_kg_extraction_job(
                lease_seconds=self.lease_seconds,
            )
        except Exception:
            logger.exception("KG extraction worker claim failed")
            return False
        if job is None:
            return False

        try:
            result = self.admin_app.process_kg_extraction_job_step(job)
        except Exception:
            logger.exception(
                "KG extraction job failed before failure persistence: %s",
                job["id"],
            )
            raise
        if result.get("phase") == "failed":
            logger.error(
                "KG extraction job failed: %s: %s",
                job["id"],
                result.get("error") or "unknown error",
            )
        return True

    async def run(self) -> None:
        """异步等待任务；同步数据库和 Chat 调用统一放到工作线程。"""
        while not self._stop_event.is_set():
            processed = await asyncio.to_thread(self.run_available_once)
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        """请求 worker 停止；空闲事件等待会被立即唤醒。"""
        self._stop_event.set()
