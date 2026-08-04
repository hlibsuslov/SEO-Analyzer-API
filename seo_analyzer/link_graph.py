import asyncio
import hashlib
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from seo_analyzer.analyzer import AnalysisArtifact, Analyzer
from seo_analyzer.crawler import SiteCrawler
from seo_analyzer.fetcher import FetchError
from seo_analyzer.models import Severity
from seo_analyzer.utils import normalize_url, origin_for, same_site, utc_now_iso

SKIP_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".bmp",
    ".avif",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".rar",
    ".7z",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".webm",
    ".wav",
    ".ogg",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".json",
    ".xml",
}


@dataclass(slots=True)
class UnifiedCrawlOptions:
    max_pages: int = 100
    max_depth: int = 5
    concurrency: int = 4
    respect_robots: bool = True
    include_subdomains: bool = False
    include_query_parameters: bool = False

    def normalized(self, server_max_pages: int) -> "UnifiedCrawlOptions":
        return UnifiedCrawlOptions(
            max_pages=max(1, min(int(self.max_pages), int(server_max_pages))),
            max_depth=max(0, min(int(self.max_depth), 25)),
            concurrency=max(1, min(int(self.concurrency), 16)),
            respect_robots=bool(self.respect_robots),
            include_subdomains=bool(self.include_subdomains),
            include_query_parameters=bool(self.include_query_parameters),
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
    parsed_start, _ = await analyzer.fetcher.validate(start_url)
    start = normalize_url(str(parsed_start), keep_query=options.include_query_parameters)
    root_domain = urlsplit(start).hostname or ""
    crawler = SiteCrawler(analyzer)
    robots = await crawler._fetch_robots(origin_for(start))

    queue: deque[tuple[str, int]] = deque([(start, 0)])
    queued: set[str] = {start}
    visited: set[str] = set()
    pages: dict[str, dict[str, Any]] = {}
    semaphore = asyncio.Semaphore(options.concurrency)

    async def analyze_one(url: str, depth: int) -> tuple[str, int, str, Any]:
        if options.respect_robots and not robots.can_fetch(
            analyzer.settings.robots_user_agent, url
        ):
            return "blocked", depth, url, None
        async with semaphore:
            try:
                artifact = await analyzer.analyze_artifact(
                    url, include_subdomains=options.include_subdomains
                )
                return "ok", depth, url, artifact
            except FetchError as exc:
                return "error", depth, url, exc
            except Exception as exc:  # pragma: no cover - defensive isolation
                return "error", depth, url, exc

    while queue and len(visited) < options.max_pages:
        _raise_if_cancelled(should_cancel)
        batch: list[tuple[str, int]] = []
        while (
            queue
            and len(batch) < options.concurrency
            and len(visited) + len(batch) < options.max_pages
        ):
            url, depth = queue.popleft()
            if url in visited or depth > options.max_depth:
                continue
            visited.add(url)
            batch.append((url, depth))
            _progress(on_progress, len(visited), url, len(queue))
        if not batch:
            continue

        results = await asyncio.gather(*(analyze_one(url, depth) for url, depth in batch))
        for status, depth, requested_url, payload in results:
            _raise_if_cancelled(should_cancel)
            if status == "blocked":
                pages[requested_url] = _blocked_page(requested_url, depth)
                continue
            if status == "error":
                pages[requested_url] = _error_page(requested_url, depth, payload)
                continue

            artifact: AnalysisArtifact = payload
            final_url = normalize_url(
                artifact.report.final_url,
                keep_query=options.include_query_parameters,
            )
            if not same_site(final_url, start, include_subdomains=options.include_subdomains):
                pages[requested_url] = _redirect_out_of_scope_page(requested_url, final_url, depth)
                continue
            pages[final_url] = _page_from_artifact(final_url, depth, artifact)
            if final_url != requested_url:
                pages[requested_url] = {
                    "url": requested_url,
                    "graph_node_id": graph_node_id(requested_url),
                    "title": "",
                    "status": 300,
                    "depth": depth,
                    "redirected_to": final_url,
                    "internal_links": [],
                    "external_links": [],
                    "incoming_links": [],
                    "seo": {
                        "score": 86,
                        "grade": "B",
                        "issues": [
                            {
                                "severity": "medium",
                                "code": "redirect",
                                "message": f"URL redirects to {final_url}",
                            }
                        ],
                    },
                }

            if depth < options.max_depth:
                for link in artifact.parsed.internal_urls:
                    try:
                        normalized = normalize_url(
                            link, keep_query=options.include_query_parameters
                        )
                    except (ValueError, UnicodeError):
                        continue
                    if not _is_html_like(normalized):
                        continue
                    if not same_site(
                        normalized, start, include_subdomains=options.include_subdomains
                    ):
                        continue
                    if normalized not in visited and normalized not in queued:
                        queued.add(normalized)
                        queue.append((normalized, depth + 1))

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
        "robots": robots.public_dict(),
        "options": {
            "max_pages": options.max_pages,
            "max_depth": options.max_depth,
            "concurrency": options.concurrency,
            "respect_robots": options.respect_robots,
            "include_subdomains": options.include_subdomains,
            "include_query_parameters": options.include_query_parameters,
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
    redirects = []
    broken_links = []

    for url, page in pages.items():
        if page.get("redirected_to"):
            redirects.append(
                {
                    "url": url,
                    "redirected_to": page["redirected_to"],
                    "status": page.get("status", 0),
                }
            )

    for source, page in real.items():
        outbound[source] = len(page.get("internal_links", []))
        for entry in page.get("internal_links", []):
            target = entry["url"]
            zones = entry.get("zones") or ["content"]
            if target in pages and pages[target].get("redirected_to"):
                target = pages[target]["redirected_to"]
            if source == target:
                continue
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
        "total_internal_links": len(edges_internal),
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
        "cycles": find_cycles(real.keys(), edges_internal),
        "duplicate_titles": _duplicates(
            (page.get("title") or "").strip().lower() for page in real.values()
        ),
        "duplicate_descriptions": _duplicates(
            ((page.get("meta") or {}).get("description") or "").strip().lower()
            for page in real.values()
        ),
        "max_depth": max((int(page.get("depth") or 0) for page in real.values()), default=0),
        "average_seo_score": _average(
            (page.get("seo") or {}).get("score") for page in real.values()
        ),
        "edges_internal": edges_internal,
    }


def build_graph(crawl: dict[str, Any], stats: dict[str, Any] | None = None) -> dict[str, Any]:
    pages = crawl.get("pages", {})
    stats = stats or build_stats(crawl)
    real = {url: page for url, page in pages.items() if "redirected_to" not in page}
    nodes = []
    for url, page in real.items():
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
                "inbound": len(page.get("incoming_links") or []),
                "internal_out": len(page.get("internal_links") or []),
                "external_out": len(page.get("external_links") or []),
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
        if source in real and target in real:
            edges.append(
                {
                    "source": real[source].get("graph_node_id") or graph_node_id(source),
                    "target": real[target].get("graph_node_id") or graph_node_id(target),
                    "source_url": source,
                    "target_url": target,
                    "type": "internal",
                    "zones": zones,
                }
            )
    external_edges: list[dict[str, Any]] = []
    for source, page in real.items():
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


def _page_from_artifact(url: str, depth: int, artifact: AnalysisArtifact) -> dict[str, Any]:
    report = artifact.report
    links = report.links
    internal_links = [
        {"url": item["url"], "zones": ["content"]} for item in links["internal"]["items"]
    ]
    external_links = [
        {"url": item["url"], "zones": ["content"]} for item in links["external"]["items"]
    ]
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
    incoming: dict[str, list[dict[str, Any]]] = {url: [] for url in real}
    title_counts = Counter((page.get("title") or "").strip().lower() for page in real.values())
    desc_counts = Counter(
        ((page.get("meta") or {}).get("description") or "").strip().lower()
        for page in real.values()
    )
    for source, page in real.items():
        for entry in page.get("internal_links", []):
            target = pages.get(entry["url"], {}).get("redirected_to") or entry["url"]
            if target in incoming and target != source:
                incoming[target].append({"url": source, "zones": entry.get("zones") or ["content"]})
            target_page = pages.get(target)
            if target_page and int(target_page.get("status") or 0) >= 400:
                _add_issue(
                    page, "high", "broken_internal_link", f"Internal link points to {target}"
                )
    for url, page in real.items():
        page["incoming_links"] = incoming.get(url, [])
        page["inbound_internal_count"] = len(page["incoming_links"])
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
    if not any(issue.get("code") == code for issue in issues):
        issues.append({"severity": severity, "code": code, "message": message})
    penalties = {
        Severity.CRITICAL.value: 20,
        Severity.HIGH.value: 12,
        Severity.MEDIUM.value: 7,
        Severity.LOW.value: 3,
        Severity.INFO.value: 1,
    }
    score = 100 - sum(penalties.get(issue.get("severity"), 2) for issue in issues)
    seo["score"] = max(0, min(float(seo.get("score", score)), score))


def _blocked_page(url: str, depth: int) -> dict[str, Any]:
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
            "issues": [
                {"severity": "medium", "code": "robots_blocked", "message": "Blocked by robots.txt"}
            ],
        },
    }


def _error_page(url: str, depth: int, exc: Exception) -> dict[str, Any]:
    message = getattr(exc, "message", str(exc))
    code = getattr(exc, "code", "fetch_error")
    status_code = getattr(exc, "status_code", 0)
    return {
        "url": url,
        "graph_node_id": graph_node_id(url),
        "title": "",
        "status": status_code if isinstance(status_code, int) else 0,
        "depth": depth,
        "internal_links": [],
        "external_links": [],
        "incoming_links": [],
        "error": message,
        "seo": {
            "score": 0,
            "issues": [{"severity": "high", "code": code, "message": message}],
        },
    }


def _redirect_out_of_scope_page(source: str, target: str, depth: int) -> dict[str, Any]:
    page = _error_page(
        source, depth, FetchError("out_of_scope_redirect", f"Redirected to {target}")
    )
    page["redirected_to"] = target
    return page


def _is_html_like(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return not any(path.endswith(extension) for extension in SKIP_EXTENSIONS)


def _raise_if_cancelled(should_cancel: Any) -> None:
    if should_cancel is not None and should_cancel():
        raise UnifiedCrawlCancelledError("Scan cancelled")


def _progress(on_progress: Any, pages_crawled: int, current_url: str, queued: int) -> None:
    if on_progress is not None:
        on_progress({"pages_crawled": pages_crawled, "current_url": current_url, "queued": queued})


def _duplicates(values: Any) -> list[dict[str, Any]]:
    counts = Counter(value for value in values if value)
    return [{"value": value, "count": count} for value, count in counts.items() if count > 1]


def _average(values: Any) -> float:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 2) if clean else 0.0


def _short_path(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
