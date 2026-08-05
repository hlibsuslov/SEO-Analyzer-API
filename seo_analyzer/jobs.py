import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from seo_analyzer.analyzer import Analyzer
from seo_analyzer.link_graph import (
    UnifiedCrawlCancelledError,
    UnifiedCrawlOptions,
    crawl_unified_site,
)
from seo_analyzer.storage import ScanStorage

LOGGER = logging.getLogger("seo_analyzer.jobs")


class UnifiedScanManager:
    def __init__(self, storage: ScanStorage, analyzer: Analyzer) -> None:
        self.storage = storage
        self.analyzer = analyzer
        self.worker_id = f"api-{uuid.uuid4()}"
        self._semaphore = asyncio.Semaphore(analyzer.settings.scan_job_workers)
        self._lease_seconds = analyzer.settings.scan_job_lease_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._reconcile_task: asyncio.Task[None] | None = None
        self._shutting_down = False

    async def startup(self) -> None:
        await self._reconcile_once()
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._reconcile_task:
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
        for event in self._cancel_events.values():
            event.set()
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    def create_and_start(
        self, project_id: str, start_url: str, options: UnifiedCrawlOptions
    ) -> dict[str, Any]:
        normalized = options.normalized(self.analyzer.settings.max_site_pages)
        scan = self.storage.create_scan(project_id, start_url, _options_dict(normalized))
        self.start(scan["id"])
        return self.storage.get_scan(scan["id"]) or scan

    def start(self, scan_id: str) -> None:
        if self._shutting_down or scan_id in self._tasks:
            return
        event = asyncio.Event()
        self._cancel_events[scan_id] = event
        self._tasks[scan_id] = asyncio.create_task(self._run(scan_id, event))

    def cancel(self, scan_id: str) -> bool:
        if not self.storage.request_cancel(scan_id):
            return False
        event = self._cancel_events.get(scan_id)
        if event:
            event.set()
        task = self._tasks.get(scan_id)
        if task and not task.done():
            task.cancel()
        return True

    def rerun(self, scan_id: str) -> dict[str, Any]:
        scan = self.storage.get_scan(scan_id)
        if not scan:
            raise ValueError(f"Scan not found: {scan_id}")
        return self.create_and_start(
            scan["project_id"],
            scan["start_url"],
            UnifiedCrawlOptions(**scan["options"]),
        )

    async def _run(self, scan_id: str, cancel_event: asyncio.Event) -> None:
        claimed = False
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            async with self._semaphore:
                if cancel_event.is_set() or self.storage.cancellation_requested(scan_id):
                    return
                claimed = self.storage.claim_scan(scan_id, self.worker_id)
                if not claimed:
                    return
                scan = self.storage.get_scan(scan_id)
                if not scan:
                    return
                heartbeat_task = asyncio.create_task(self._heartbeat_loop(scan_id))
                options = UnifiedCrawlOptions(**scan["options"])
                LOGGER.info("scan_started scan_id=%s", scan_id)

                def report_progress(progress: dict[str, Any]) -> None:
                    self.storage.update_progress(scan_id, progress, worker_id=self.worker_id)

                result = await crawl_unified_site(
                    self.analyzer,
                    scan["start_url"],
                    options,
                    on_progress=report_progress,
                    should_cancel=lambda: (
                        cancel_event.is_set() or self.storage.cancellation_requested(scan_id)
                    ),
                )
                if cancel_event.is_set() or self.storage.cancellation_requested(scan_id):
                    self.storage.finish_owned_scan(scan_id, self.worker_id, status="cancelled")
                else:
                    saved = self.storage.save_result(scan_id, result, worker_id=self.worker_id)
                    if saved:
                        LOGGER.info("scan_completed scan_id=%s", scan_id)
                    elif self.storage.cancellation_requested(scan_id):
                        self.storage.finish_owned_scan(scan_id, self.worker_id, status="cancelled")
        except UnifiedCrawlCancelledError:
            if claimed:
                self.storage.finish_owned_scan(scan_id, self.worker_id, status="cancelled")
        except asyncio.CancelledError:
            if claimed:
                if self._shutting_down:
                    self.storage.release_owned_scan(
                        scan_id,
                        self.worker_id,
                        error="Interrupted by service shutdown; queued for automatic recovery",
                    )
                else:
                    self.storage.finish_owned_scan(scan_id, self.worker_id, status="cancelled")
            raise
        except Exception as exc:  # pragma: no cover - final job boundary
            LOGGER.exception("scan_failed scan_id=%s", scan_id)
            if claimed:
                self.storage.finish_owned_scan(
                    scan_id,
                    self.worker_id,
                    status="failed",
                    error=str(exc),
                )
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            self._tasks.pop(scan_id, None)
            self._cancel_events.pop(scan_id, None)

    async def _heartbeat_loop(self, scan_id: str) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not self.storage.heartbeat(scan_id, self.worker_id):
                return

    async def _reconcile_loop(self) -> None:
        interval = max(1.0, min(5.0, self._lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            await self._reconcile_once()

    async def _reconcile_once(self) -> None:
        stale_before = (
            (datetime.now(UTC) - timedelta(seconds=self._lease_seconds))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        for scan_id in self.storage.recover_stale_scans(stale_before):
            self.start(scan_id)


def _options_dict(options: UnifiedCrawlOptions) -> dict[str, Any]:
    return {
        "max_pages": options.max_pages,
        "max_depth": options.max_depth,
        "concurrency": options.concurrency,
        "respect_robots": options.respect_robots,
        "include_subdomains": options.include_subdomains,
        "include_query_parameters": options.include_query_parameters,
        "use_sitemap": options.use_sitemap,
    }
