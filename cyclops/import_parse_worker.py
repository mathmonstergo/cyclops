from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any


logger = logging.getLogger(__name__)


class ImportParseWorker:
    """在 ASGI 生命周期内推进持久解析任务，关闭时可控结束。"""

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
        self._lease_renew_interval_seconds = min(lease_seconds / 3, 30.0)
        self._stop_event = asyncio.Event()

    def run_available_once(self) -> bool:
        """领取一个到期任务交给业务层推进；单项异常不终止后续循环。"""
        job = self.admin_app.database().claim_import_parse_job(
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False
        lease_stop = threading.Event()
        lease_thread = threading.Thread(
            target=self._renew_lease_until_stopped,
            args=(job, lease_stop),
            name=f"cyclops-import-parse-lease-{job['id']}",
            daemon=True,
        )
        lease_thread.start()
        try:
            self.admin_app.process_import_parse_job(job)
        except Exception:
            logger.exception("import parse job failed unexpectedly: %s", job["id"])
        finally:
            lease_stop.set()
            lease_thread.join()
        return True

    def _renew_lease_until_stopped(
        self,
        job: dict[str, Any],
        stop_event: threading.Event,
    ) -> None:
        """在同步 provider 调用期间续租，失去 token 后停止写 lease。"""
        while not stop_event.wait(self._lease_renew_interval_seconds):
            try:
                renewed = self.admin_app.database().renew_import_parse_job_lease(
                    job["id"],
                    lease_token=job["lease_token"],
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                logger.exception("failed to renew import parse job lease: %s", job["id"])
                continue
            if not renewed:
                logger.warning("import parse job lease is no longer current: %s", job["id"])
                return

    async def run(self) -> None:
        """循环领取到期任务，同步 provider 调用统一放入工作线程。"""
        while not self._stop_event.is_set():
            try:
                processed = await asyncio.to_thread(self.run_available_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("import parse worker claim loop failed")
                processed = False
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
        """请求 worker 停止；空闲等待会被事件立即唤醒。"""
        self._stop_event.set()
