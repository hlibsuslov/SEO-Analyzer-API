import asyncio
from typing import Any

from seo_analyzer.analyzer import Analyzer
from seo_analyzer.link_graph import (
    UnifiedCrawlCancelledError,
    UnifiedCrawlOptions,
    crawl_unified_site,
)
from seo_analyzer.storage import ScanStorage
from seo_analyzer.utils import utc_now_iso


class UnifiedScanManager:
    def __init__(self, storage: ScanStorage, analyzer: Analyzer) -> None:
        self.storage = storage
        self.analyzer = analyzer
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}

    def create_and_start(
        self, project_id: str, start_url: str, options: UnifiedCrawlOptions
    ) -> dict[str, Any]:
        normalized = options.normalized(self.analyzer.settings.max_site_pages)
        scan = self.storage.create_scan(project_id, start_url, _options_dict(normalized))
        self.start(scan["id"])
        return self.storage.get_scan(scan["id"]) or scan

    def start(self, scan_id: str) -> None:
        if scan_id in self._tasks:
            return
        event = asyncio.Event()
        self._cancel_events[scan_id] = event
        self._tasks[scan_id] = asyncio.create_task(self._run(scan_id, event))

    def cancel(self, scan_id: str) -> bool:
        scan = self.storage.get_scan(scan_id)
        if not scan or scan["status"] not in {"pending", "running", "cancelling"}:
            return False
        event = self._cancel_events.get(scan_id)
        if event:
            event.set()
        task = self._tasks.get(scan_id)
        if task and not task.done():
            task.cancel()
        self.storage.update_scan(scan_id, status="cancelling")
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
        scan = self.storage.get_scan(scan_id)
        if not scan:
            return
        options = UnifiedCrawlOptions(**scan["options"])
        self.storage.update_scan(scan_id, status="running", started_at=utc_now_iso(), error=None)
        try:
            result = await crawl_unified_site(
                self.analyzer,
                scan["start_url"],
                options,
                on_progress=lambda progress: self.storage.update_progress(scan_id, progress),
                should_cancel=cancel_event.is_set,
            )
            if cancel_event.is_set():
                self.storage.update_scan(scan_id, status="cancelled", finished_at=utc_now_iso())
            else:
                self.storage.save_result(scan_id, result)
        except (UnifiedCrawlCancelledError, asyncio.CancelledError):
            self.storage.update_scan(scan_id, status="cancelled", finished_at=utc_now_iso())
        except Exception as exc:  # pragma: no cover - final job boundary
            self.storage.update_scan(
                scan_id,
                status="failed",
                error=str(exc),
                finished_at=utc_now_iso(),
            )
        finally:
            self._tasks.pop(scan_id, None)
            self._cancel_events.pop(scan_id, None)


def _options_dict(options: UnifiedCrawlOptions) -> dict[str, Any]:
    return {
        "max_pages": options.max_pages,
        "max_depth": options.max_depth,
        "concurrency": options.concurrency,
        "respect_robots": options.respect_robots,
        "include_subdomains": options.include_subdomains,
        "include_query_parameters": options.include_query_parameters,
    }
