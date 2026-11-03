import sqlite3

import pytest

from seo_analyzer.storage import ScanStorage


def test_connections_commit_rollback_and_close(tmp_path) -> None:
    storage = ScanStorage(tmp_path / "storage.db")
    try:
        with storage.connect() as committed:
            committed.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?)",
                ("saved", "Saved", "https://site.test/", "2026-11-03T00:00:00Z"),
            )
        assert storage.get_project("saved") is not None
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            committed.execute("SELECT 1")

        with pytest.raises(ValueError, match="abort"), storage.connect() as rolled_back:
            rolled_back.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?)",
                ("aborted", "Aborted", "https://site.test/", "2026-11-03T00:00:00Z"),
            )
            raise ValueError("abort")
        assert storage.get_project("aborted") is None
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            rolled_back.execute("SELECT 1")
    finally:
        storage.close()


def test_shutdown_preserves_persisted_cancellation() -> None:
    storage = ScanStorage(":memory:")
    try:
        project = storage.create_project("https://site.test/")
        scan = storage.create_scan(project["id"], project["root_url"], {})
        assert storage.claim_scan(scan["id"], "owner")
        assert storage.request_cancel(scan["id"])
        assert storage.release_owned_scan(scan["id"], "owner", error="shutdown")
        saved = storage.get_scan(scan["id"])
        assert saved["status"] == "cancelled"
        assert saved["finished_at"] is not None
        assert saved["error"] is None
        assert storage.recover_stale_scans("2099-01-01T00:00:00Z") == []
    finally:
        storage.close()


def test_stale_worker_cannot_publish_a_result() -> None:
    storage = ScanStorage(":memory:")
    try:
        project = storage.create_project("https://site.test/")
        scan = storage.create_scan(project["id"], project["root_url"], {})
        assert storage.claim_scan(scan["id"], "old-worker")
        storage.recover_stale_scans("2099-01-01T00:00:00Z")
        assert storage.claim_scan(scan["id"], "new-worker")
        result = {"crawl": {"pages": {}}, "stats": {"total_pages": 0}}
        assert storage.save_result(scan["id"], result, worker_id="old-worker") is False
        assert storage.get_scan(scan["id"])["status"] == "running"
        assert storage.get_result(scan["id"]) is None
        assert storage.save_result(scan["id"], result, worker_id="new-worker") is True
        assert storage.get_scan(scan["id"])["status"] == "completed"
    finally:
        storage.close()
