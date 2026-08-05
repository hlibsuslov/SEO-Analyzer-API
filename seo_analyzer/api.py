import asyncio
import hmac
import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest
from prometheus_client import Counter as PrometheusCounter
from pydantic import BaseModel, Field, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from seo_analyzer import __version__
from seo_analyzer.analyzer import Analyzer
from seo_analyzer.config import Settings, get_settings
from seo_analyzer.crawler import SiteCrawler
from seo_analyzer.dashboard import render_dashboard
from seo_analyzer.fetcher import FetchError
from seo_analyzer.frontend import APP_HTML
from seo_analyzer.jobs import UnifiedScanManager
from seo_analyzer.link_graph import UnifiedCrawlOptions, build_graph
from seo_analyzer.models import CompareRequest, OpportunityRequest, PageAnalysis, SiteAuditRequest
from seo_analyzer.opportunity import rank_opportunities
from seo_analyzer.storage import ScanStorage
from seo_analyzer.utils import normalize_url, utc_now_iso

LOGGER = logging.getLogger("seo_analyzer.api")
REQUESTS = PrometheusCounter(
    "seo_analyzer_http_requests_total", "HTTP requests", ("method", "route", "status")
)
REQUEST_SECONDS = Histogram(
    "seo_analyzer_http_request_duration_seconds", "HTTP request duration", ("method", "route")
)
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
MAX_REQUEST_BODY_BYTES = 1_000_000


class RequestBodyTooLargeError(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request bodies are limited to 1 MB")


class RequestBodyLimitMiddleware:
    """Enforce the body budget for both Content-Length and chunked requests."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared_length = headers.get(b"content-length", b"")
        if declared_length.isdigit() and int(declared_length) > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLargeError:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        request_id = scope.get("state", {}).get("request_id")
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "request_too_large",
                    "message": "Request bodies are limited to 1 MB",
                    "request_id": request_id,
                }
            },
        )
        await response(scope, receive, send)


class ProjectCreate(BaseModel):
    url: str = Field(min_length=4, max_length=2_048)
    name: str | None = Field(default=None, max_length=200)


class ScanCreate(BaseModel):
    url: str | None = Field(default=None, min_length=4, max_length=2_048)
    max_pages: int = Field(default=100, ge=1, le=1_000)
    max_depth: int = Field(default=5, ge=0, le=25)
    concurrency: int = Field(default=4, ge=1, le=16)
    respect_robots: bool = True
    include_subdomains: bool = False
    include_query_parameters: bool = False
    use_sitemap: bool = True


def create_app(settings: Settings | None = None, analyzer: Analyzer | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    owns_analyzer = analyzer is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=getattr(logging, runtime_settings.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        app.state.settings = runtime_settings
        app.state.analyzer = analyzer or Analyzer(runtime_settings)
        app.state.crawler = SiteCrawler(app.state.analyzer)
        app.state.scan_storage = ScanStorage(runtime_settings.scan_storage_path)
        app.state.scan_manager = UnifiedScanManager(
            app.state.scan_storage,
            app.state.analyzer,
        )
        await app.state.scan_manager.startup()
        try:
            yield
        finally:
            await app.state.scan_manager.shutdown()
            app.state.scan_storage.close()
            if owns_analyzer:
                await app.state.analyzer.close()

    app = FastAPI(
        title="SaaS SEO Analyzer API",
        summary="Security-first technical SEO, SaaS strategy, crawl, comparison, and opportunity diagnostics",
        description=(
            "Audits public web pages without claiming to predict rankings. Core SEO health and SaaS "
            "acquisition/conversion maturity are scored separately with evidence and prioritized fixes."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_tags=[
            {"name": "analysis", "description": "Page and site SEO diagnostics"},
            {"name": "strategy", "description": "Comparison and first-party opportunity ranking"},
            {"name": "compatibility", "description": "Backwards-compatible v1 endpoints"},
            {"name": "operations", "description": "Health and observability"},
        ],
    )
    if runtime_settings.parsed_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=runtime_settings.parsed_cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
        )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied_request_id = request.headers.get("x-request-id", "")
        request_id = (
            supplied_request_id
            if SAFE_REQUEST_ID.fullmatch(supplied_request_id)
            else str(uuid.uuid4())
        )
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        REQUESTS.labels(request.method, route_path, response.status_code).inc()
        REQUEST_SECONDS.labels(request.method, route_path).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        LOGGER.info(
            "request method=%s route=%s status=%s duration_ms=%s request_id=%s",
            request.method,
            route_path,
            response.status_code,
            round(elapsed * 1_000),
            request_id,
        )
        return response

    @app.exception_handler(FetchError)
    async def fetch_error_handler(request: Request, exc: FetchError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    @app.exception_handler(RequestBodyTooLargeError)
    async def body_limit_error_handler(
        request: Request, _exc: RequestBodyTooLargeError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "request_too_large",
                    "message": "Request bodies are limited to 1 MB",
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    async def require_api_key(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        expected = runtime_settings.api_key
        if expected is None or not expected.get_secret_value():
            return
        supplied = x_api_key or ""
        if not hmac.compare_digest(supplied, expected.get_secret_value()):
            raise FetchError("unauthorized", "A valid X-API-Key is required", status_code=401)

    def get_analyzer(request: Request) -> Analyzer:
        return request.app.state.analyzer

    def get_crawler(request: Request) -> SiteCrawler:
        return request.app.state.crawler

    def get_scan_storage(request: Request) -> ScanStorage:
        return request.app.state.scan_storage

    def get_scan_manager(request: Request) -> UnifiedScanManager:
        return request.app.state.scan_manager

    @app.get("/", tags=["operations"])
    async def root() -> dict[str, Any]:
        return {
            "name": "SaaS SEO Analyzer API",
            "version": __version__,
            "status": "ok",
            "documentation": "/docs",
            "endpoints": {
                "page_analysis": "GET /v1/analyze?url=https://example.com",
                "site_audit": "POST /v1/site-audit",
                "comparison": "POST /v1/compare",
                "opportunity_ranking": "POST /v1/opportunities",
            },
        }

    @app.get("/healthz", tags=["operations"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "time": utc_now_iso()}

    @app.get("/readyz", tags=["operations"])
    async def ready(current: Analyzer = Depends(get_analyzer)) -> dict[str, Any]:
        return {"status": "ready", "cache_entries": await current.cache.size()}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get(
        "/v1/analyze",
        tags=["analysis"],
        dependencies=[Depends(require_api_key)],
        response_model=PageAnalysis,
    )
    async def analyze_v1(
        url: str = Query(min_length=4, max_length=2_048),
        include_pagespeed: bool = Query(default=False),
        include_subdomains: bool = Query(default=False),
        current: Analyzer = Depends(get_analyzer),
    ) -> PageAnalysis:
        report = await current.analyze(
            url,
            include_pagespeed=include_pagespeed,
            include_subdomains=include_subdomains,
        )
        return report

    @app.post("/v1/site-audit", tags=["analysis"], dependencies=[Depends(require_api_key)])
    async def site_audit(
        payload: SiteAuditRequest,
        crawler: SiteCrawler = Depends(get_crawler),
    ) -> dict[str, Any]:
        return await crawler.audit(payload)

    @app.post("/v1/compare", tags=["strategy"], dependencies=[Depends(require_api_key)])
    async def compare(
        payload: CompareRequest,
        current: Analyzer = Depends(get_analyzer),
    ) -> dict[str, Any]:
        async def analyze_one(url: str) -> tuple[str, Any]:
            try:
                report = await current.analyze(url, include_pagespeed=payload.include_pagespeed)
                return "ok", report
            except FetchError as exc:
                return "error", {"url": url, "code": exc.code, "error": exc.message}

        results = await asyncio.gather(*(analyze_one(url) for url in payload.urls))
        reports = [value for status, value in results if status == "ok"]
        errors = [value for status, value in results if status == "error"]
        if len(reports) < 2:
            raise FetchError(
                "insufficient_comparable_pages",
                "At least two URLs must be fetched successfully",
                status_code=422,
            )
        seo_ranking = sorted(reports, key=lambda report: -report.score.overall)
        saas_ranking = sorted(
            (report for report in reports if report.saas["score"]["overall"] is not None),
            key=lambda report: -report.saas["score"]["overall"],
        )
        issue_frequency = Counter(issue.code for report in reports for issue in report.issues)
        category_leaders = {}
        for category in reports[0].score.categories:
            leader = max(reports, key=lambda report: report.score.categories[category])
            category_leaders[category] = {
                "url": leader.final_url,
                "score": leader.score.categories[category],
            }
        return {
            "schema_version": "2.0",
            "compared_at": utc_now_iso(),
            "rankings": {
                "seo_health": [
                    {
                        "rank": index,
                        "url": report.final_url,
                        "score": report.score.overall,
                        "grade": report.score.grade,
                    }
                    for index, report in enumerate(seo_ranking, 1)
                ],
                "saas_conversion_readiness": [
                    {
                        "rank": index,
                        "url": report.final_url,
                        "score": report.saas["score"]["overall"],
                        "grade": report.saas["score"]["grade"],
                    }
                    for index, report in enumerate(saas_ranking, 1)
                ],
                "category_leaders": category_leaders,
                "note": "Relative page diagnostics only; this is not a SERP or market-share ranking.",
            },
            "common_gaps": [
                {"issue_code": code, "pages": count}
                for code, count in issue_frequency.most_common()
                if count >= 2
            ],
            "analyses": [report.model_dump(mode="json") for report in reports],
            "errors": errors,
        }

    @app.post("/v1/opportunities", tags=["strategy"], dependencies=[Depends(require_api_key)])
    async def opportunities(payload: OpportunityRequest) -> dict[str, Any]:
        return rank_opportunities(payload)

    @app.get("/analyze", tags=["compatibility"], dependencies=[Depends(require_api_key)])
    async def legacy_analyze(
        url: str = Query(min_length=4, max_length=2_048),
        current: Analyzer = Depends(get_analyzer),
    ) -> dict[str, Any]:
        report = await current.analyze(url)
        return _legacy_payload(report)

    @app.get("/quick-score", tags=["compatibility"], dependencies=[Depends(require_api_key)])
    async def legacy_quick_score(
        url: str = Query(min_length=4, max_length=2_048),
        current: Analyzer = Depends(get_analyzer),
    ) -> dict[str, Any]:
        report = await current.analyze(url)
        return {
            "url": report.final_url,
            "seo_score": report.score.overall,
            "seo_grade": report.score.grade,
            "seo_warnings": [issue.title for issue in report.issues if issue.severity != "info"],
            "top_recommendations": [
                recommendation.model_dump(mode="json")
                for recommendation in report.recommendations[:5]
            ],
        }

    @app.get("/metadata", tags=["compatibility"], dependencies=[Depends(require_api_key)])
    async def legacy_metadata(
        url: str = Query(min_length=4, max_length=2_048),
        current: Analyzer = Depends(get_analyzer),
    ) -> dict[str, Any]:
        report = await current.analyze(url)
        return {
            "url": report.final_url,
            "title": report.metadata["title"],
            "meta": {
                "description": report.metadata["description"],
                "keywords": report.metadata["keywords"],
                "canonical": report.metadata["canonical"],
                "robots": report.indexability["robots_meta"],
            },
            "headings": report.headings,
            "social": report.social,
        }

    @app.get("/app", response_class=HTMLResponse, tags=["link-graph"])
    async def graph_app() -> HTMLResponse:
        return HTMLResponse(APP_HTML)

    @app.get("/api/health", tags=["link-graph"])
    async def graph_health(
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> dict[str, Any]:
        return {"status": "ok", "storage": str(storage.path)}

    @app.post(
        "/api/projects",
        status_code=201,
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def create_project(
        payload: ProjectCreate,
        current: Analyzer = Depends(get_analyzer),
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> dict[str, Any]:
        root_url = await _validated_scan_url(current, payload.url)
        return storage.create_project(root_url, payload.name)

    @app.get(
        "/api/projects",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def list_projects(
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> list[dict[str, Any]]:
        return storage.list_projects()

    @app.get(
        "/api/projects/{project_id}",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def get_project(
        project_id: str,
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> dict[str, Any]:
        project = storage.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    @app.post(
        "/api/projects/{project_id}/scans",
        status_code=202,
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def start_scan(
        project_id: str,
        payload: ScanCreate,
        current: Analyzer = Depends(get_analyzer),
        storage: ScanStorage = Depends(get_scan_storage),
        manager: UnifiedScanManager = Depends(get_scan_manager),
    ) -> dict[str, Any]:
        project = storage.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        start_url = await _validated_scan_url(current, payload.url or project["root_url"])
        options = UnifiedCrawlOptions(
            max_pages=payload.max_pages,
            max_depth=payload.max_depth,
            concurrency=payload.concurrency,
            respect_robots=payload.respect_robots,
            include_subdomains=payload.include_subdomains,
            include_query_parameters=payload.include_query_parameters,
            use_sitemap=payload.use_sitemap,
        )
        return manager.create_and_start(project_id, start_url, options)

    @app.get(
        "/api/projects/{project_id}/scans",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def list_project_scans(
        project_id: str,
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> list[dict[str, Any]]:
        if not storage.get_project(project_id):
            raise HTTPException(status_code=404, detail="Project not found")
        return storage.list_scans(project_id)

    @app.get(
        "/api/scans/{scan_id}/status",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def scan_status(
        scan_id: str,
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> dict[str, Any]:
        return _require_scan(scan_id, storage)

    @app.post(
        "/api/scans/{scan_id}/cancel",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def cancel_scan(
        scan_id: str,
        storage: ScanStorage = Depends(get_scan_storage),
        manager: UnifiedScanManager = Depends(get_scan_manager),
    ) -> dict[str, bool]:
        _require_scan(scan_id, storage)
        return {"cancelled": manager.cancel(scan_id)}

    @app.post(
        "/api/scans/{scan_id}/rerun",
        status_code=202,
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def rerun_scan(
        scan_id: str,
        manager: UnifiedScanManager = Depends(get_scan_manager),
    ) -> dict[str, Any]:
        try:
            return manager.rerun(scan_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Scan not found") from None

    @app.get(
        "/api/scans/{scan_id}/pages",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def scan_pages(
        scan_id: str,
        include_redirects: bool = False,
        status: int | None = None,
        max_depth: int | None = None,
        issue: str | None = None,
        min_score: float | None = None,
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> list[dict[str, Any]]:
        _require_scan(scan_id, storage)
        pages = storage.list_pages(scan_id, include_redirects=include_redirects)
        return _filter_pages(
            pages,
            include_redirects=include_redirects,
            status=status,
            max_depth=max_depth,
            issue=issue,
            min_score=min_score,
        )

    @app.get(
        "/api/scans/{scan_id}/page",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def scan_page_by_url(
        scan_id: str,
        url: str = Query(min_length=4, max_length=2_048),
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> dict[str, Any]:
        scan = _require_scan(scan_id, storage)
        page = storage.get_page_by_url(
            scan_id,
            normalize_url(
                url,
                keep_query=bool(scan["options"].get("include_query_parameters", False)),
            ),
        )
        if not page:
            raise HTTPException(status_code=404, detail="Page not found")
        return page

    @app.get(
        "/api/scans/{scan_id}/pages/{graph_node_id}",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def scan_page_by_node(
        scan_id: str,
        graph_node_id: str,
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> dict[str, Any]:
        _require_scan(scan_id, storage)
        page = storage.get_page_by_node_id(scan_id, graph_node_id)
        if not page:
            raise HTTPException(status_code=404, detail="Page not found")
        return page

    @app.get(
        "/api/scans/{scan_id}/links",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def scan_links(
        scan_id: str,
        type: str | None = Query(default=None, pattern="^(internal|external|redirect)$"),
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> list[dict[str, Any]]:
        _require_scan(scan_id, storage)
        return storage.list_links(scan_id, type)

    @app.get(
        "/api/scans/{scan_id}/graph",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def scan_graph(
        scan_id: str,
        status: int | None = None,
        max_depth: int | None = None,
        issue: str | None = None,
        min_score: float | None = None,
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> dict[str, Any]:
        result = _require_result(scan_id, storage)
        if any(value is not None for value in (status, max_depth, issue, min_score)):
            pages = _filter_pages(
                list(result["crawl"]["pages"].values()),
                include_redirects=True,
                status=status,
                max_depth=max_depth,
                issue=issue,
                min_score=min_score,
            )
            allowed = {page["url"] for page in pages}
            filtered_crawl = {
                **result["crawl"],
                "pages": {
                    url: page for url, page in result["crawl"]["pages"].items() if url in allowed
                },
            }
            return build_graph(filtered_crawl)
        return result["graph"]

    @app.get(
        "/api/scans/{scan_id}/seo/issues",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def scan_issues(
        scan_id: str,
        severity: str | None = None,
        code: str | None = None,
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> list[dict[str, Any]]:
        _require_scan(scan_id, storage)
        issues = storage.list_issues(scan_id, severity=severity)
        return [item for item in issues if item.get("code") == code] if code else issues

    @app.get(
        "/api/scans/{scan_id}/stats",
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def scan_stats(
        scan_id: str,
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> dict[str, Any]:
        return _require_result(scan_id, storage)["stats"]

    @app.get(
        "/api/scans/{scan_id}/dashboard",
        response_class=HTMLResponse,
        tags=["link-graph"],
        dependencies=[Depends(require_api_key)],
    )
    async def scan_dashboard(
        scan_id: str,
        storage: ScanStorage = Depends(get_scan_storage),
    ) -> HTMLResponse:
        return HTMLResponse(render_dashboard(_require_result(scan_id, storage)))

    return app


def _legacy_payload(report: Any) -> dict[str, Any]:
    h1 = [item["text"] for item in report.headings["items"] if item["level"] == 1]
    h2 = [item["text"] for item in report.headings["items"] if item["level"] == 2]
    return {
        "url": report.final_url,
        "title": report.metadata["title"],
        "meta": {
            "description": report.metadata["description"],
            "keywords": report.metadata["keywords"],
        },
        "headings": {
            "h1": {"count": len(h1), "texts": h1},
            "h2": {"count": len(h2), "texts": h2},
        },
        "links": {
            "internal": [item["url"] for item in report.links["internal"]["items"]],
            "external": [item["url"] for item in report.links["external"]["items"]],
        },
        "images": {
            "total": report.images["total"],
            "with_alt": report.images["with_descriptive_alt"],
            "without_alt": report.images["missing_alt_attribute"],
        },
        "word_count": report.content["word_count"],
        "word_density_percent": {
            item["term"]: item["density_percent"] for item in report.content["top_terms"]
        },
        "load_time_ms": report.fetch["timing"]["total_ms"],
        "seo_warnings": [issue.title for issue in report.issues if issue.severity != "info"],
        "seo_score": report.score.overall,
        "seo_grade": report.score.grade,
        "v2": report.model_dump(mode="json"),
    }


async def _validated_scan_url(analyzer: Analyzer, url: str) -> str:
    parsed, _ = await analyzer.fetcher.validate(url)
    return normalize_url(str(parsed), keep_query=True)


def _require_scan(scan_id: str, storage: ScanStorage) -> dict[str, Any]:
    scan = storage.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    return scan


def _require_result(scan_id: str, storage: ScanStorage) -> dict[str, Any]:
    scan = _require_scan(scan_id, storage)
    result = storage.get_result(scan_id)
    if result is None:
        raise HTTPException(status_code=409, detail=f"Scan is not complete: {scan['status']}")
    return result


def _filter_pages(
    pages: list[dict[str, Any]],
    *,
    include_redirects: bool = False,
    status: int | None = None,
    max_depth: int | None = None,
    issue: str | None = None,
    min_score: float | None = None,
) -> list[dict[str, Any]]:
    filtered = []
    for page in pages:
        if page.get("redirected_to") and not include_redirects:
            continue
        if status is not None and int(page.get("status") or 0) != status:
            continue
        if max_depth is not None and (page.get("depth") is None or int(page["depth"]) > max_depth):
            continue
        seo = page.get("seo") or {}
        if min_score is not None and float(seo.get("score") or 0) < min_score:
            continue
        if issue and not any(item.get("code") == issue for item in seo.get("issues") or []):
            continue
        filtered.append(page)
    return filtered


try:
    app = create_app()
except ValidationError as exc:  # pragma: no cover - fail-fast deployment configuration
    raise RuntimeError(f"Invalid SEO Analyzer configuration: {exc}") from exc
