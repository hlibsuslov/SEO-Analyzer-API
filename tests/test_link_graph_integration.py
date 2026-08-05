from collections import Counter

import httpx
import pytest
from conftest import make_fetcher, make_settings

from seo_analyzer.analyzer import Analyzer
from seo_analyzer.dashboard import render_dashboard
from seo_analyzer.link_graph import (
    UnifiedCrawlOptions,
    _add_issue,
    build_graph,
    crawl_unified_site,
)


def test_dashboard_escapes_inline_script_breakout() -> None:
    attack = "</script><script>globalThis.PWNED=1</script>"
    result = {
        "stats": {"root_domain": "safe.test"},
        "crawl": {"pages": {}},
        "graph": {"nodes": [{"label": attack}], "edges": []},
    }
    markup = render_dashboard(result)
    assert attack not in markup
    assert "\\u003c/script\\u003e" in markup


def test_site_level_issue_recalculates_grade() -> None:
    page = {"seo": {"score": 90, "grade": "A", "issues": []}}
    for code in ("duplicate_title", "duplicate_description", "orphan"):
        _add_issue(page, "medium", code, code)
    assert page["seo"]["score"] == 69
    assert page["seo"]["grade"] == "D"


def test_filtered_graph_drops_dangling_edge_stats() -> None:
    root = "https://site.test/"
    graph = build_graph(
        {
            "start_url": root,
            "root_domain": "site.test",
            "pages": {
                root: {
                    "url": root,
                    "graph_node_id": "root",
                    "status": 200,
                    "depth": 0,
                    "incoming_links": [],
                    "internal_links": [{"url": "https://site.test/filtered", "zones": ["nav"]}],
                    "external_links": [],
                    "seo": {"score": 90, "grade": "A", "issues": []},
                }
            },
        }
    )
    assert graph["edges"] == []
    assert graph["stats"]["total_internal_links"] == 0
    assert graph["stats"]["total_discovered_internal_links"] == 1


@pytest.mark.asyncio
async def test_unified_crawl_deduplicates_redirect_and_discovers_sitemap_orphan() -> None:
    requests: Counter[str] = Counter()
    root_html = b"""<html><head><title>Home</title></head><body>
    <header><nav><a href='/'>Home</a></nav></header></body></html>"""
    orphan_html = b"<html><head><title>Orphan</title></head><body><h1>Orphan</h1></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        key = f"{request.url.scheme}://{host}{request.url.path}"
        requests[key] += 1
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="Sitemap: https://site.test/sitemap.xml\n",
            )
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                headers={"content-type": "application/xml"},
                text="<urlset><url><loc>https://site.test/orphan</loc></url></urlset>",
            )
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"location": "https://site.test/"})
        if request.url.path == "/orphan":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=orphan_html)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=root_html)

    settings = make_settings(cache_ttl_seconds=60, max_site_pages=10)
    analyzer = Analyzer(settings, fetcher=make_fetcher(handler, settings=settings))
    try:
        result = await crawl_unified_site(
            analyzer,
            "http://site.test/",
            UnifiedCrawlOptions(max_pages=5, max_depth=1, use_sitemap=True),
        )
    finally:
        await analyzer.close()

    assert requests["https://site.test/"] == 1
    assert result["crawl"]["pages"]["http://site.test/"]["status"] == 301
    assert "https://site.test/orphan" in result["stats"]["orphan_pages"]
    root = result["crawl"]["pages"]["https://site.test/"]
    assert root["internal_links"][0]["zones"] == ["header"]


@pytest.mark.asyncio
async def test_fetch_errors_do_not_become_fake_target_http_statuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, headers={"content-type": "text/plain"})
        raise httpx.ConnectError("network unavailable", request=request)

    settings = make_settings(max_site_pages=2)
    analyzer = Analyzer(settings, fetcher=make_fetcher(handler, settings=settings))
    try:
        result = await crawl_unified_site(
            analyzer,
            "https://site.test/",
            UnifiedCrawlOptions(max_pages=1, use_sitemap=False),
        )
    finally:
        await analyzer.close()
    page = result["crawl"]["pages"]["https://site.test/"]
    assert page["status"] == 0
    assert page["seo"]["issues"][0]["code"] == "upstream_unreachable"
