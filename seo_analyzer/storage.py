import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from seo_analyzer.utils import utc_now_iso


class ScanStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root_url TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scans (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    start_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pages_crawled INTEGER NOT NULL DEFAULT 0,
                    queued INTEGER NOT NULL DEFAULT 0,
                    current_url TEXT NOT NULL DEFAULT '',
                    options_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS pages (
                    scan_id TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                    url TEXT NOT NULL,
                    graph_node_id TEXT NOT NULL,
                    status INTEGER NOT NULL DEFAULT 0,
                    title TEXT NOT NULL DEFAULT '',
                    depth INTEGER NOT NULL DEFAULT 0,
                    seo_score REAL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (scan_id, url)
                );
                CREATE INDEX IF NOT EXISTS idx_pages_scan_node
                    ON pages(scan_id, graph_node_id);
                CREATE TABLE IF NOT EXISTS links (
                    scan_id TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                    source_url TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    type TEXT NOT NULL,
                    zones_json TEXT NOT NULL,
                    PRIMARY KEY (scan_id, source_url, target_url, type, zones_json)
                );
                CREATE INDEX IF NOT EXISTS idx_links_scan_source
                    ON links(scan_id, source_url);
                CREATE INDEX IF NOT EXISTS idx_links_scan_target
                    ON links(scan_id, target_url);
                """
            )

    def create_project(self, root_url: str, name: str | None = None) -> dict[str, Any]:
        project = {
            "id": str(uuid.uuid4()),
            "name": name or root_url,
            "root_url": root_url,
            "created_at": utc_now_iso(),
        }
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO projects(id, name, root_url, created_at) VALUES (?, ?, ?, ?)",
                (project["id"], project["name"], project["root_url"], project["created_at"]),
            )
        return project

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def create_scan(
        self, project_id: str, start_url: str, options: dict[str, Any]
    ) -> dict[str, Any]:
        scan = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "start_url": start_url,
            "status": "pending",
            "pages_crawled": 0,
            "queued": 0,
            "current_url": "",
            "options": options,
            "error": None,
            "created_at": utc_now_iso(),
            "started_at": None,
            "finished_at": None,
        }
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scans(
                    id, project_id, start_url, status, pages_crawled, queued,
                    current_url, options_json, error, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan["id"],
                    project_id,
                    start_url,
                    scan["status"],
                    scan["pages_crawled"],
                    scan["queued"],
                    scan["current_url"],
                    json.dumps(options),
                    scan["error"],
                    scan["created_at"],
                    scan["started_at"],
                    scan["finished_at"],
                ),
            )
        return scan

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        return self._scan_from_row(row) if row else None

    def list_scans(self, project_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scans"
        params: tuple[Any, ...] = ()
        if project_id:
            sql += " WHERE project_id = ?"
            params = (project_id,)
        sql += " ORDER BY created_at DESC"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._scan_from_row(row) for row in rows]

    def update_scan(self, scan_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "pages_crawled",
            "queued",
            "current_url",
            "result_json",
            "error",
            "started_at",
            "finished_at",
        }
        assignments = []
        params = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Cannot update scan field: {key}")
            assignments.append(f"{key} = ?")
            params.append(value)
        if not assignments:
            return
        params.append(scan_id)
        with self._lock, self.connect() as conn:
            conn.execute(
                f"UPDATE scans SET {', '.join(assignments)} WHERE id = ?",  # noqa: S608
                params,
            )

    def update_progress(self, scan_id: str, progress: dict[str, Any]) -> None:
        self.update_scan(
            scan_id,
            pages_crawled=int(progress.get("pages_crawled") or 0),
            queued=int(progress.get("queued") or 0),
            current_url=progress.get("current_url") or "",
        )

    def save_result(self, scan_id: str, result: dict[str, Any]) -> None:
        pages = result.get("crawl", {}).get("pages", {})
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM pages WHERE scan_id = ?", (scan_id,))
            conn.execute("DELETE FROM links WHERE scan_id = ?", (scan_id,))
            for url, page in pages.items():
                seo = page.get("seo") or {}
                conn.execute(
                    """
                    INSERT INTO pages(
                        scan_id, url, graph_node_id, status, title, depth, seo_score, data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id,
                        url,
                        page.get("graph_node_id") or url,
                        int(page.get("status") or 0),
                        page.get("title") or "",
                        int(page.get("depth") or 0),
                        seo.get("score"),
                        json.dumps(page, ensure_ascii=False),
                    ),
                )
                if page.get("redirected_to"):
                    continue
                for entry in page.get("internal_links", []):
                    self._insert_link(conn, scan_id, url, entry, "internal")
                for entry in page.get("external_links", []):
                    self._insert_link(conn, scan_id, url, entry, "external")
            conn.execute(
                """
                UPDATE scans
                SET status = ?, result_json = ?, pages_crawled = ?, finished_at = ?, error = NULL
                WHERE id = ?
                """,
                (
                    "completed",
                    json.dumps(result, ensure_ascii=False),
                    int((result.get("stats") or {}).get("total_pages") or 0),
                    utc_now_iso(),
                    scan_id,
                ),
            )

    def get_result(self, scan_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT result_json FROM scans WHERE id = ?", (scan_id,)).fetchone()
        return json.loads(row["result_json"]) if row and row["result_json"] else None

    def list_pages(self, scan_id: str, include_redirects: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT data_json FROM pages WHERE scan_id = ? ORDER BY depth ASC, url ASC",
                (scan_id,),
            ).fetchall()
        pages = [json.loads(row["data_json"]) for row in rows]
        return (
            pages
            if include_redirects
            else [page for page in pages if not page.get("redirected_to")]
        )

    def get_page_by_node_id(self, scan_id: str, node_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT data_json FROM pages WHERE scan_id = ? AND graph_node_id = ?",
                (scan_id, node_id),
            ).fetchone()
        return json.loads(row["data_json"]) if row else None

    def get_page_by_url(self, scan_id: str, url: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT data_json FROM pages WHERE scan_id = ? AND url = ?",
                (scan_id, url),
            ).fetchone()
        return json.loads(row["data_json"]) if row else None

    def list_links(self, scan_id: str, link_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT source_url, target_url, type, zones_json FROM links WHERE scan_id = ?"
        params: list[Any] = [scan_id]
        if link_type:
            sql += " AND type = ?"
            params.append(link_type)
        sql += " ORDER BY source_url, target_url"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "source_url": row["source_url"],
                "target_url": row["target_url"],
                "type": row["type"],
                "zones": json.loads(row["zones_json"]),
            }
            for row in rows
        ]

    def list_issues(self, scan_id: str, severity: str | None = None) -> list[dict[str, Any]]:
        issues = []
        for page in self.list_pages(scan_id):
            seo = page.get("seo") or {}
            for issue in seo.get("issues") or []:
                if severity and issue.get("severity") != severity:
                    continue
                issues.append(
                    {
                        "url": page.get("url"),
                        "graph_node_id": page.get("graph_node_id"),
                        "status": page.get("status", 0),
                        "depth": page.get("depth", 0),
                        "seo_score": seo.get("score"),
                        **issue,
                    }
                )
        return issues

    def _insert_link(
        self,
        conn: sqlite3.Connection,
        scan_id: str,
        source_url: str,
        entry: dict[str, Any],
        link_type: str,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO links(scan_id, source_url, target_url, type, zones_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                source_url,
                entry["url"],
                link_type,
                json.dumps(entry.get("zones") or ["content"]),
            ),
        )

    def _scan_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        scan = dict(row)
        scan["options"] = json.loads(scan.pop("options_json") or "{}")
        scan.pop("result_json", None)
        return scan
