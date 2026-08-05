import asyncio
import time

import httpx
from conftest import OPTIMIZED_HTML, make_fetcher, make_settings
from fastapi.testclient import TestClient

from seo_analyzer.analyzer import Analyzer
from seo_analyzer.api import create_app


def build_client(
    *,
    api_key: str | None = None,
    cors: str = "",
    scan_storage_path: str | None = None,
) -> tuple[TestClient, Analyzer]:
    settings = make_settings(
        api_key=api_key,
        cors_origins=cors,
        cache_ttl_seconds=60,
        **({"scan_storage_path": scan_storage_path} if scan_storage_path else {}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/robots.txt", "/sitemap.xml"}:
            return httpx.Response(404, headers={"content-type": "text/plain"})
        html = OPTIMIZED_HTML.replace(b"saas.test", request.headers["host"].encode())
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": "br"},
            content=html,
        )

    analyzer = Analyzer(settings, fetcher=make_fetcher(handler, settings=settings))
    return TestClient(create_app(settings, analyzer=analyzer)), analyzer


def close_analyzer(analyzer: Analyzer) -> None:
    asyncio.run(analyzer.close())


def test_operations_analysis_and_legacy_endpoints() -> None:
    client, analyzer = build_client()
    try:
        with client:
            root = client.get("/")
            assert root.status_code == 200
            assert root.json()["version"] == "2.0.0"
            assert client.get("/healthz").json()["status"] == "ok"
            assert client.get("/readyz").json()["status"] == "ready"
            metrics = client.get("/metrics")
            assert metrics.status_code == 200
            assert "seo_analyzer_http_requests_total" in metrics.text

            response = client.get(
                "/v1/analyze",
                params={"url": "https://saas.test"},
                headers={"X-Request-ID": "test-request"},
            )
            assert response.status_code == 200
            assert response.headers["x-request-id"] == "test-request"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.json()["schema_version"] == "2.0"
            assert response.json()["saas"]["score"]["applicable"] is True

            unsafe_id = client.get(
                "/v1/analyze",
                params={"url": "https://saas.test"},
                headers={"X-Request-ID": "unsafe request id"},
            )
            assert unsafe_id.headers["x-request-id"] != "unsafe request id"
            assert len(unsafe_id.headers["x-request-id"]) == 36

            legacy = client.get("/analyze", params={"url": "https://saas.test"})
            assert legacy.status_code == 200
            assert legacy.json()["seo_score"] >= 90
            assert "v2" in legacy.json()
            assert legacy.json()["v2"]["schema_version"] == "2.0"
            assert legacy.json()["v2"]["score"]["grade"] == "A"
            quick = client.get("/quick-score", params={"url": "https://saas.test"})
            assert quick.json()["seo_grade"] == "A"
            metadata = client.get("/metadata", params={"url": "https://saas.test"})
            assert metadata.json()["meta"]["canonical"] == "https://saas.test/"
    finally:
        close_analyzer(analyzer)


def test_site_compare_and_opportunity_endpoints() -> None:
    client, analyzer = build_client()
    try:
        with client:
            site = client.post(
                "/v1/site-audit",
                json={"url": "https://saas.test", "max_pages": 3, "use_sitemap": False},
            )
            assert site.status_code == 200
            assert site.json()["sample"]["pages_audited"] >= 1

            comparison = client.post(
                "/v1/compare",
                json={
                    "urls": [
                        "https://saas.test",
                        "https://other.test/features/automation",
                    ]
                },
            )
            assert comparison.status_code == 200
            assert len(comparison.json()["rankings"]["seo_health"]) == 2
            assert "not a SERP" in comparison.json()["rankings"]["note"]

            opportunities = client.post(
                "/v1/opportunities",
                json={
                    "pages": [
                        {
                            "url": "https://saas.test/pricing",
                            "impressions": 1000,
                            "clicks": 20,
                            "average_position": 8,
                        }
                    ]
                },
            )
            assert opportunities.status_code == 200
            assert opportunities.json()["pages"][0]["rank"] == 1
    finally:
        close_analyzer(analyzer)


def test_auth_validation_fetch_errors_and_request_limit() -> None:
    client, analyzer = build_client(api_key="top-secret")
    try:
        with client:
            unauthorized = client.get("/v1/analyze", params={"url": "https://saas.test"})
            assert unauthorized.status_code == 401
            assert unauthorized.json()["error"]["code"] == "unauthorized"
            authorized = client.get(
                "/v1/analyze",
                params={"url": "https://saas.test"},
                headers={"X-API-Key": "top-secret"},
            )
            assert authorized.status_code == 200

            private = client.get(
                "/v1/analyze",
                params={"url": "http://127.0.0.1"},
                headers={"X-API-Key": "top-secret", "X-Request-ID": "private-test"},
            )
            assert private.status_code == 422
            assert private.json()["error"]["code"] == "private_network_blocked"
            assert private.json()["error"]["request_id"] == "private-test"

            invalid = client.post(
                "/v1/compare",
                json={"urls": ["https://saas.test"]},
                headers={"X-API-Key": "top-secret"},
            )
            assert invalid.status_code == 422

            too_large = client.post(
                "/v1/opportunities",
                content="x" * 1_000_001,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": "top-secret",
                },
            )
            assert too_large.status_code == 413
            assert too_large.json()["error"]["code"] == "request_too_large"

            chunked = client.post(
                "/v1/opportunities",
                content=(chunk for chunk in (b"x" * 600_000, b"y" * 600_000)),
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": "top-secret",
                    "Transfer-Encoding": "chunked",
                },
            )
            assert chunked.status_code == 413
            assert chunked.json()["error"]["code"] == "request_too_large"
    finally:
        close_analyzer(analyzer)


def test_configured_cors_preflight() -> None:
    client, analyzer = build_client(cors="https://dashboard.test")
    try:
        with client:
            response = client.options(
                "/v1/analyze",
                headers={
                    "Origin": "https://dashboard.test",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == ("https://dashboard.test")
    finally:
        close_analyzer(analyzer)


def test_unified_link_graph_scan_endpoints(tmp_path) -> None:
    client, analyzer = build_client(scan_storage_path=str(tmp_path / "scans.db"))
    try:
        with client:
            project = client.post("/api/projects", json={"url": "https://saas.test"}).json()
            scan = client.post(
                f"/api/projects/{project['id']}/scans",
                json={"max_pages": 3, "max_depth": 1, "concurrency": 2, "respect_robots": False},
            ).json()
            status = {}
            for _ in range(30):
                time.sleep(0.05)
                status = client.get(f"/api/scans/{scan['id']}/status").json()
                if status["status"] in {"completed", "failed", "cancelled"}:
                    break
            assert status["status"] == "completed"

            stats = client.get(f"/api/scans/{scan['id']}/stats").json()
            assert stats["total_pages"] >= 1
            assert stats["total_internal_links"] >= 1

            graph = client.get(f"/api/scans/{scan['id']}/graph").json()
            assert graph["nodes"]
            assert any(node["seo_score"] is not None for node in graph["nodes"])

            pages = client.get(f"/api/scans/{scan['id']}/pages").json()
            assert pages[0]["graph_node_id"]
            node_detail = client.get(f"/api/scans/{scan['id']}/pages/{pages[0]['graph_node_id']}")
            assert node_detail.status_code == 200

            assert client.get(f"/api/scans/{scan['id']}/seo/issues").status_code == 200
            dashboard = client.get(f"/api/scans/{scan['id']}/dashboard")
            assert dashboard.status_code == 200
            assert "Link Graph" in dashboard.text
    finally:
        close_analyzer(analyzer)


def test_link_graph_ui_uses_api_key_for_dashboard_fetch(tmp_path) -> None:
    client, analyzer = build_client(
        api_key="graph-secret",
        scan_storage_path=str(tmp_path / "protected-scans.db"),
    )
    try:
        with client:
            assert (
                client.post("/api/projects", json={"url": "https://saas.test"}).status_code == 401
            )
            assert (
                client.post(
                    "/api/projects",
                    headers={"X-API-Key": "graph-secret"},
                    json={"url": "https://saas.test"},
                ).status_code
                == 201
            )
            app = client.get("/app")
            assert app.status_code == 200
            assert "headers: headers(false)" in app.text
            assert 'href="/api/scans/' not in app.text
    finally:
        close_analyzer(analyzer)
