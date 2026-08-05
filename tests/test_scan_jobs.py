import asyncio

import pytest
from conftest import make_settings

from seo_analyzer.analyzer import Analyzer
from seo_analyzer.jobs import UnifiedScanManager
from seo_analyzer.link_graph import UnifiedCrawlCancelledError, UnifiedCrawlOptions
from seo_analyzer.storage import ScanStorage


def test_memory_storage_and_stale_scan_recovery() -> None:
    storage = ScanStorage(":memory:")
    try:
        project = storage.create_project("https://site.test/")
        scan = storage.create_scan(project["id"], project["root_url"], {})
        storage.update_scan(
            scan["id"],
            status="running",
            worker_id="dead-worker",
            heartbeat_at="2020-01-01T00:00:00Z",
        )
        assert storage.recover_stale_scans("2021-01-01T00:00:00Z") == [scan["id"]]
        assert storage.get_scan(scan["id"])["status"] == "pending"
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_scan_manager_bounds_workers_and_requeues_on_shutdown(tmp_path, monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_crawl(*_args, should_cancel, **_kwargs):
        started.set()
        while not release.is_set():
            if should_cancel():
                raise UnifiedCrawlCancelledError("cancelled")
            await asyncio.sleep(0.01)
        return {
            "crawl": {"pages": {}},
            "stats": {"total_pages": 0},
            "graph": {"nodes": [], "edges": []},
        }

    monkeypatch.setattr("seo_analyzer.jobs.crawl_unified_site", fake_crawl)
    settings = make_settings(scan_job_workers=1, scan_job_lease_seconds=10)
    analyzer = Analyzer(settings)
    storage = ScanStorage(tmp_path / "jobs.db")
    manager = UnifiedScanManager(storage, analyzer)
    project = storage.create_project("https://site.test/")
    await manager.startup()
    try:
        first = manager.create_and_start(project["id"], project["root_url"], UnifiedCrawlOptions())
        second = manager.create_and_start(project["id"], project["root_url"], UnifiedCrawlOptions())
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert storage.get_scan(first["id"])["status"] == "running"
        assert storage.get_scan(second["id"])["status"] == "pending"
        await manager.shutdown()
        assert storage.get_scan(first["id"])["status"] == "pending"
        assert storage.get_scan(second["id"])["status"] == "pending"
    finally:
        release.set()
        storage.close()
        await analyzer.close()


@pytest.mark.asyncio
async def test_cancel_is_observed_across_managers(tmp_path, monkeypatch) -> None:
    started = asyncio.Event()

    async def fake_crawl(*_args, should_cancel, **_kwargs):
        started.set()
        while not should_cancel():
            await asyncio.sleep(0.01)
        raise UnifiedCrawlCancelledError("cancelled")

    monkeypatch.setattr("seo_analyzer.jobs.crawl_unified_site", fake_crawl)
    settings = make_settings(scan_job_workers=1, scan_job_lease_seconds=10)
    analyzer = Analyzer(settings)
    storage = ScanStorage(tmp_path / "shared.db")
    owner = UnifiedScanManager(storage, analyzer)
    peer = UnifiedScanManager(storage, analyzer)
    project = storage.create_project("https://site.test/")
    await owner.startup()
    try:
        scan = owner.create_and_start(project["id"], project["root_url"], UnifiedCrawlOptions())
        await asyncio.wait_for(started.wait(), timeout=1)
        assert peer.cancel(scan["id"]) is True
        for _ in range(100):
            if storage.get_scan(scan["id"])["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)
        assert storage.get_scan(scan["id"])["status"] == "cancelled"
    finally:
        await owner.shutdown()
        storage.close()
        await analyzer.close()
