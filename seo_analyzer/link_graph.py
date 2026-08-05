import hashlib
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from seo_analyzer.analyzer import AnalysisArtifact, Analyzer
from seo_analyzer.crawler import SiteCrawlCancelledError, SiteCrawler, SiteCrawlRequest
from seo_analyzer.models import Severity
from seo_analyzer.utils import grade_for, normalize_url, utc_now_iso


@dataclass(slots=True)
class UnifiedCrawlOptions:
    max_pages: int = 100
    max_depth: int = 5
    concurrency: int = 4
    respect_robots: bool = True
    include_subdomains: bool = False
    include_query_parameters: bool = False
    use_sitemap: bool = True

    def normalized(self, server_max_pages: int) -> "UnifiedCrawlOptions":
        return UnifiedCrawlOptions(
            max_pages=max(1, min(int(self.max_pages), int(server_max_pages))),
            max_depth=max(0, min(int(self.max_depth), 25)),
            concurrency=max(1, min(int(self.concurrency), 16)),
            respect_robots=bool(self.respect_robots),
            include_subdomains=bool(self.include_subdomains),
            include_query_parameters=bool(self.include_query_parameters),
            use_sitemap=bool(self.use_sitemap),
        )


class UnifiedCrawlCancelledError(RuntimeError):
    pass


async def crawl_unified_site(
    analyzer: Analyzer,
    start_url: str,
    options: UnifiedCrawlOptions | None = None,
    *,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    options = (options or UnifiedCrawlOptions()).normalized(analyzer.settings.max_site_pages)
    crawler = SiteCrawler(analyzer)
    request = SiteCrawlRequest(
        url=start_url,
        max_pages=options.max_pages,
        max_depth=options.max_depth,
        concurrency=options.concurrency,
        respect_robots=options.respect_robots,
        include_subdomains=options.include_subdomains,
        include_query_parameters=options.include_query_parameters,
        use_sitemap=options.use_sitemap,
    )
    try:
        snapshot = await crawler.crawl(
            request,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
    except SiteCrawlCancelledError as exc:
        raise UnifiedCrawlCancelledError(str(exc)) from exc

    start = snapshot.start_url
    root_domain = urlsplit(start).hostname or ""
    pages: dict[str, dict[str, Any]] = {}

    for final_url, record in snapshot.crawled.items():
        pages[final_url] = _page_from_artifact(
            final_url,
            record.depth,
            record.artifact,
            keep_query=options.include_query_parameters,
        )
    for source, redirect in snapshot.redirects.items():
        pages[source] = _redirect_page(
            source,
            redirect["target"],
            redirect.get("depth"),
            int(redirect.get("status_code") or 300),
            redirect.get("chain") or [],
        )
    for url in snapshot.blocked:
        context = snapshot.url_context.get(url) or {}
        pages.setdefault(url, _blocked_page(url, context.get("depth")))
    for failure in snapshot.failures:
        pages.setdefault(
            failure["url"],
            _error_page_data(
                failure["url"],
                failure.get("depth"),
                failure.get("code") or "fetch_error",
                failure.get("error") or "Page fetch failed",
                failure.get("upstream_status_code"),
            ),
        )
    for redirect in snapshot.out_of_scope_redirects:
        pages.setdefault(
            redirect["source"],
            _redirect_out_of_scope_page(
                redirect["source"],
                redirect["target"],
                redirect.get("depth"),
                int(redirect.get("status_code") or 300),
            ),
        )

    crawl = {"start_url": start, "root_domain": root_domain, "pages": pages}
    _apply_incoming_and_site_issues(crawl)
    stats = build_stats(crawl)
    graph = build_graph(crawl, stats)
    return {
        "schema_version": "3.0",
        "generated_at": utc_now_iso(),
        "crawl": crawl,
        "stats": stats,
        "graph": graph,
        "robots": snapshot.robots.public_dict(),
        "sitemap": {key: value for key, value in snapshot.sitemap.items() if key != "urls"}
        | {"url_samples": snapshot.sitemap.get("urls", [])[:100]},
        "options": {
            "max_pages": options.max_pages,
            "max_depth": options.max_depth,
            "concurrency": options.concurrency,
            "respect_robots": options.respect_robots,
            "include_subdomains": options.include_subdomains,
            "include_query_parameters": options.include_query_parameters,
            "use_sitemap": options.use_sitemap,
        },
    }


def graph_node_id(url: str) -> str:
    return "p_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def build_stats(crawl: dict[str, Any]) -> dict[str, Any]:
    pages = crawl.get("pages", {})
    real = {url: page for url, page in pages.items() if "redirected_to" not in page}
    inbound: Counter[str] = Counter()
    outbound: Counter[str] = Counter()
    external_domains: Counter[str] = Counter()
    edges_internal: list[list[Any]] = []
    redirect_edges: list[list[str]] = []
    redirects = []
    broken_links = []
    discovered_internal_links = 0

    for url, page in pages.items():
        if page.get("redirected_to"):
            target = page["redirected_to"]
            redirects.append(
                {
                    "url": url,
                    "redirected_to": target,
                    "status": page.get("status", 0),
                }
            )
            if target in pages:
                inbound[target] += 1
                outbound[url] = 1
                redirect_edges.append([url, target])

    for source, page in real.items():
        outbound[source] = 0
        for entry in page.get("internal_links", []):
            discovered_internal_links += 1
            target = entry["url"]
            zones = entry.get("zones") or ["content"]
            if source == target or target not in pages:
                continue
            outbound[source] += 1
            inbound[target] += 1
            edges_internal.append([source, target, zones])
            target_page = pages.get(target)
            if target_page and int(target_page.get("status") or 0) >= 400:
                broken_links.append(
                    {
                        "source_url": source,
                        "target_url": target,
                        "status": target_page.get("status"),
                        "zones": zones,
                    }
                )
        for entry in page.get("external_links", []):
            host = urlsplit(entry["url"]).hostname or ""
            if host:
                external_domains[host.lower()] += 1

    orphan_pages = [
        url for url in real if inbound.get(url, 0) == 0 and url != crawl.get("start_url")
    ]
    dead_end_pages = [url for url in real if outbound.get(url, 0) == 0]
    broken_pages = [
        [url, page.get("status", 0)]
        for url, page in real.items()
        if int(page.get("status") or 0) >= 400 or int(page.get("status") or 0) == 0
    ]
    return {
        "root_domain": crawl.get("root_domain", ""),
        "start_url": crawl.get("start_url", ""),
        "total_pages": len(real),
        "total_graph_nodes": len(pages),
        "total_internal_links": len(edges_internal),
        "total_discovered_internal_links": discovered_internal_links,
        "total_external_links": sum(external_domains.values()),
        "unique_external_domains": len(external_domains),
        "inbound": dict(inbound),
        "outbound": dict(outbound),
        "top_inbound": inbound.most_common(25),
        "top_outbound": outbound.most_common(25),
        "top_external_domains": external_domains.most_common(25),
        "orphan_pages": orphan_pages,
        "dead_end_pages": dead_end_pages,
        "broken_pages": broken_pages,
        "broken_links": broken_links,
        "redirects": redirects,
        "cycles": find_cycles(
            pages.keys(),
            [
                *edges_internal,
                *([source, target, ["redirect"]] for source, target in redirect_edges),
            ],
        ),
        "duplicate_titles": _duplicates(
            (page.get("title") or "").strip().lower() for page in real.values()
        ),
        "duplicate_descriptions": _duplicates(
            ((page.get("meta") or {}).get("description") or "").strip().lower()
            for page in real.values()
        ),
        "max_depth": max(
            (int(page["depth"]) for page in real.values() if page.get("depth") is not None),
            default=0,
        ),
        "average_seo_score": _average(
            (page.get("seo") or {}).get("score") for page in real.values()
        ),
        "edges_internal": edges_internal,
        "edges_redirect": redirect_edges,
    }


def build_graph(crawl: dict[str, Any], stats: dict[str, Any] | None = None) -> dict[str, Any]:
    pages = crawl.get("pages", {})
    stats = stats or build_stats(crawl)
    nodes = []
    for url, page in pages.items():
        seo = page.get("seo") or {}
        issues = seo.get("issues") or []
        nodes.append(
            {
                "id": page.get("graph_node_id") or graph_node_id(url),
                "url": url,
                "label": page.get("title") or _short_path(url),
                "status": page.get("status", 0),
                "content_type": page.get("content_type", ""),
                "depth": page.get("depth", 0),
                "inbound": int((stats.get("inbound") or {}).get(url, 0)),
                "internal_out": int((stats.get("outbound") or {}).get(url, 0)),
                "external_out": len(page.get("external_links") or []),
                "redirected_to": page.get("redirected_to"),
                "seo_score": seo.get("score"),
                "seo_grade": seo.get("grade"),
                "seo_issue_count": len(issues),
                "seo_error_count": sum(
                    1 for issue in issues if issue.get("severity") in {"critical", "high"}
                ),
                "seo_warning_count": sum(
                    1 for issue in issues if issue.get("severity") in {"medium", "low"}
                ),
            }
        )
    edges = []
    for source, target, zones in stats.get("edges_internal", []):
        if source in pages and target in pages:
            edges.append(
                {
                    "source": pages[source].get("graph_node_id") or graph_node_id(source),
                    "target": pages[target].get("graph_node_id") or graph_node_id(target),
                    "source_url": source,
                    "target_url": target,
                    "type": "internal",
                    "zones": zones,
                }
            )
    for source, target in stats.get("edges_redirect", []):
        if source in pages and target in pages:
            edges.append(
                {
                    "source": pages[source].get("graph_node_id") or graph_node_id(source),
                    "target": pages[target].get("graph_node_id") or graph_node_id(target),
                    "source_url": source,
                    "target_url": target,
                    "type": "redirect",
                    "zones": ["redirect"],
                }
            )
    external_edges: list[dict[str, Any]] = []
    for source, page in pages.items():
        redirect_target = page.get("redirected_to")
        if redirect_target and redirect_target not in pages:
            external_edges.append(
                {
                    "source": page.get("graph_node_id") or graph_node_id(source),
                    "source_url": source,
                    "target_url": redirect_target,
                    "target_domain": urlsplit(redirect_target).hostname or "",
                    "type": "redirect_external",
                    "zones": ["redirect"],
                }
            )
        external_edges.extend(
            {
                "source": page.get("graph_node_id") or graph_node_id(source),
                "source_url": source,
                "target_url": entry["url"],
                "target_domain": urlsplit(entry["url"]).hostname or "",
                "type": "external",
                "zones": entry.get("zones") or ["content"],
            }
            for entry in page.get("external_links", [])
        )
    return {"nodes": nodes, "edges": edges, "external_edges": external_edges, "stats": stats}


def find_cycles(urls: Any, edges_internal: list[list[Any]], limit: int = 50) -> list[list[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    url_set = set(urls)
    for source, target, _zones in edges_internal:
        if source in url_set and target in url_set:
            graph[source].add(target)
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    cycles: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlink[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph.get(node, set()):
            if target not in indices:
                strongconnect(target)
                lowlink[node] = min(lowlink[node], lowlink[target])
            elif target in on_stack:
                lowlink[node] = min(lowlink[node], indices[target])
        if lowlink[node] == indices[node]:
            component = []
            while True:
                current = stack.pop()
                on_stack.remove(current)
                component.append(current)
                if current == node:
                    break
            if len(component) > 1:
                cycles.append(sorted(component))

    for url in sorted(url_set):
        if len(cycles) >= limit:
            break
        if url not in indices:
            strongconnect(url)
    return cycles[:limit]


def _page_from_artifact(
    url: str,
    depth: int | None,
    artifact: AnalysisArtifact,
    *,
    keep_query: bool,
) -> dict[str, Any]:
    report = artifact.report
    links = report.links
    internal_links = _normalized_link_entries(
        artifact.parsed.internal_link_records, keep_query=keep_query
    )
    external_links = _normalized_link_entries(
        artifact.parsed.external_link_records, keep_query=keep_query
    )
    return {
        "url": url,
        "graph_node_id": graph_node_id(url),
        "title": report.metadata["title"],
        "status": report.fetch["status_code"],
        "content_type": report.fetch["content_type"],
        "depth": depth,
        "response_time_ms": report.fetch["timing"]["total_ms"],
        "size_bytes": report.fetch["content_bytes"],
        "meta": {
            "description": report.metadata["description"],
            "keywords": report.metadata["keywords"],
            "canonical": report.metadata["canonical"],
            "robots": ", ".join(report.indexability["robots_meta"]),
            "h1": artifact.parsed.h1_texts[0] if artifact.parsed.h1_texts else "",
            "h1_count": len(artifact.parsed.h1_texts),
            "lang": report.international["html_lang"] or "",
            "og_image": report.social["open_graph"].get("og:image", ""),
        },
        "internal_links": internal_links,
        "external_links": external_links,
        "incoming_links": [],
        "seo": {
            "score": report.score.overall,
            "grade": report.score.grade,
            "issues": [
                {
                    "severity": issue.severity.value,
                    "code": issue.code,
                    "message": issue.title,
                    "category": issue.category,
                    "evidence": issue.evidence,
                }
                for issue in report.issues
            ],
            "recommendations": [
                recommendation.model_dump(mode="json")
                for recommendation in report.recommendations[:10]
            ],
            "headings": report.headings["items"],
            "word_count": report.content["word_count"],
            "images": report.images,
            "links": {
                "internal": len(internal_links),
                "external": len(external_links),
                "nofollow": links["nofollow"],
                "broken": [],
            },
            "response_time_ms": report.fetch["timing"]["total_ms"],
            "size_bytes": report.fetch["content_bytes"],
        },
    }


def _apply_incoming_and_site_issues(crawl: dict[str, Any]) -> None:
    pages = crawl["pages"]
    real = {url: page for url, page in pages.items() if "redirected_to" not in page}
    incoming: dict[str, list[dict[str, Any]]] = {url: [] for url in pages}
    title_counts = Counter((page.get("title") or "").strip().lower() for page in real.values())
    desc_counts = Counter(
        ((page.get("meta") or {}).get("description") or "").strip().lower()
        for page in real.values()
    )
    for source, page in real.items():
        for entry in page.get("internal_links", []):
            target = entry["url"]
            if target in incoming and target != source:
                incoming[target].append({"url": source, "zones": entry.get("zones") or ["content"]})
            target_page = pages.get(target)
            if target_page and int(target_page.get("status") or 0) >= 400:
                _add_issue(
                    page, "high", "broken_internal_link", f"Internal link points to {target}"
                )
                (page.get("seo") or {}).setdefault("links", {}).setdefault("broken", []).append(
                    {"url": target, "status": target_page.get("status")}
                )
    for source, page in pages.items():
        target = page.get("redirected_to")
        if target in incoming:
            incoming[target].append({"url": source, "zones": ["redirect"]})
    for source, page in pages.items():
        page["incoming_links"] = incoming.get(source, [])
        page["inbound_internal_count"] = len(page["incoming_links"])
    for url, page in real.items():
        title = (page.get("title") or "").strip().lower()
        desc = ((page.get("meta") or {}).get("description") or "").strip().lower()
        if title and title_counts[title] > 1:
            _add_issue(page, "medium", "title_duplicate", "Title duplicates another crawled page")
        if desc and desc_counts[desc] > 1:
            _add_issue(
                page,
                "medium",
                "description_duplicate",
                "Meta description duplicates another crawled page",
            )
        if url != crawl.get("start_url") and not page["incoming_links"]:
            _add_issue(page, "medium", "orphan_page", "No incoming internal links were found")


def _add_issue(page: dict[str, Any], severity: str, code: str, message: str) -> None:
    seo = page.setdefault("seo", {"issues": [], "score": 100})
    issues = seo.setdefault("issues", [])
    if any(issue.get("code") == code for issue in issues):
        return
    issues.append({"severity": severity, "code": code, "message": message})
    penalties = {
        Severity.CRITICAL.value: 20,
        Severity.HIGH.value: 12,
        Severity.MEDIUM.value: 7,
        Severity.LOW.value: 3,
        Severity.INFO.value: 1,
    }
    current_score = float(seo.get("score") if seo.get("score") is not None else 100)
    score = max(0.0, current_score - penalties.get(severity, 2))
    seo["score"] = score
    seo["grade"] = grade_for(score)[0]


def _blocked_page(url: str, depth: int | None) -> dict[str, Any]:
    return {
        "url": url,
        "graph_node_id": graph_node_id(url),
        "title": "",
        "status": 0,
        "depth": depth,
        "internal_links": [],
        "external_links": [],
        "incoming_links": [],
        "seo": {
            "score": 0,
            "grade": "F",
            "issues": [
                {"severity": "medium", "code": "robots_blocked", "message": "Blocked by robots.txt"}
            ],
        },
    }


def _error_page_data(
    url: str,
    depth: int | None,
    code: str,
    message: str,
    upstream_status_code: int | None = None,
) -> dict[str, Any]:
    return {
        "url": url,
        "graph_node_id": graph_node_id(url),
        "title": "",
        "status": upstream_status_code or 0,
        "depth": depth,
        "internal_links": [],
        "external_links": [],
        "incoming_links": [],
        "error": message,
        "seo": {
            "score": 0,
            "grade": "F",
            "issues": [{"severity": "high", "code": code, "message": message}],
        },
    }


def _redirect_page(
    source: str,
    target: str,
    depth: int | None,
    status_code: int,
    chain: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "url": source,
        "graph_node_id": graph_node_id(source),
        "title": "",
        "status": status_code,
        "content_type": "",
        "depth": depth,
        "redirected_to": target,
        "redirect_chain": chain,
        "internal_links": [],
        "external_links": [],
        "incoming_links": [],
        "seo": {
            "score": None,
            "grade": None,
            "issues": [
                {
                    "severity": "medium",
                    "code": "redirect",
                    "message": f"URL redirects to {target}",
                }
            ],
        },
    }


def _redirect_out_of_scope_page(
    source: str, target: str, depth: int | None, status_code: int
) -> dict[str, Any]:
    page = _redirect_page(source, target, depth, status_code, [])
    page["seo"]["issues"] = [
        {
            "severity": "high",
            "code": "out_of_scope_redirect",
            "message": f"Redirected outside the crawl scope to {target}",
        }
    ]
    return page


def _normalized_link_entries(
    records: list[dict[str, Any]], *, keep_query: bool
) -> list[dict[str, Any]]:
    merged: dict[str, set[str]] = {}
    for record in records:
        try:
            url = normalize_url(record["url"], keep_query=keep_query)
        except (KeyError, ValueError, UnicodeError):
            continue
        merged.setdefault(url, set()).update(record.get("zones") or ["content"])
    return [{"url": url, "zones": sorted(zones)} for url, zones in merged.items()]


def _duplicates(values: Any) -> list[dict[str, Any]]:
    counts = Counter(value for value in values if value)
    return [{"value": value, "count": count} for value, count in counts.items() if count > 1]


def _average(values: Any) -> float:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 2) if clean else 0.0


def _short_path(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
