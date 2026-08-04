# Link Graph Integration

This branch keeps the existing SaaS SEO Analyzer v2 architecture as the base and
adds persistent link-graph scans on top of the existing safe asynchronous
`Analyzer`.

## Decisions

- The existing `SafeFetcher` remains the only network layer. The graph scan does
  not add a second crawler with `requests`, so SSRF protections, DNS validation,
  redirect validation, response budgets, and concurrency limits remain intact.
- Each fetched HTML page is analyzed once by `Analyzer.analyze_artifact`. The
  resulting parsed links and SEO report are reused for graph nodes, edges, page
  details, issue lists, stats, and the dashboard.
- The previous `/v1/*`, `/analyze`, `/quick-score`, and `/metadata` endpoints are
  preserved. New persistent scan endpoints live under `/api/*`, and the browser
  workflow is available at `/app`.
- SQLite stores projects, scans, pages, links, and the complete JSON result
  snapshot. This is suitable for local and small-team deployments.

## New Modules

- `seo_analyzer.link_graph`: async BFS graph scan, page normalization, graph
  stats, cycles, duplicates, redirects, and SEO-to-node mapping.
- `seo_analyzer.storage`: SQLite persistence.
- `seo_analyzer.jobs`: background scan tasks, progress, cancellation, and rerun.
- `seo_analyzer.dashboard`: completed-scan graph dashboard renderer.
- `seo_analyzer.frontend`: minimal browser UI for starting scans.

## Limitations

- Jobs are in-process and are not resumed after an API process restart.
- External links are collected and visualized, but external targets are not fully
  crawled.
- The dashboard is intentionally dependency-free and compact; deeper graph
  layout algorithms can be added later if needed.
