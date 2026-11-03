# Changelog

All notable changes to this fork are documented here.

## 2.0.1 — 2026-11-03

### Changed

- Refreshed runtime and development dependency minimums against current stable
  PyPI releases; declared the directly imported Pydantic and Starlette dependencies.
- Added Python 3.12 and 3.15 to CI, covering every supported minor from 3.11 to 3.15.
- Updated the digest-pinned container base to Python 3.15.2, pinned CI actions by
  commit, and added workflow cancellation and job timeouts.
- CI now checks the installed wheel outside the checkout and starts the hardened
  container to verify health, readiness, and third-party attribution files.
- Updated Windows setup, timeout behavior, scan lifecycle, and contributor docs.

### Fixed

- Explicitly close SQLite connections after commit or rollback instead of waiting
  for garbage collection; fail tests on resource warnings.
- Keep explicit scan cancellations cancelled during service shutdown.
- Validate scan ownership and publish results in one SQLite write transaction.
- Enforce a total fetch deadline across DNS, queueing, redirects, and streaming.
- Return structured `502` errors for interrupted, malformed, and timed-out response
  streams while closing the upstream response.
- Cancel and drain shared in-flight analyses before closing HTTP clients.
- Include the project's and link-graph component's license notices when building
  the wheel inside Docker.

## 2.0.0 — 2026-07-22

### Added

- Security-first async fetcher with SSRF controls, DNS pinning, redirect revalidation, response budgets, and bounded concurrency.
- Versioned page analysis with evidence-backed issues, category scores, grades, and prioritized recommendations.
- SaaS page classification, acquisition/conversion readiness scoring, and strategy-pillar assessment.
- Robots-aware sampled site crawler with sitemap-index/gzip support, duplicates, broken-link evidence, internal-authority ranking, and orphan candidates.
- Relative multi-page comparison and first-party Search Console/conversion opportunity ranking.
- Optional Google PageSpeed Insights v5 provider with graceful quota/error handling.
- Typed request/response models, API-key option, CORS allowlist, request IDs, health/readiness routes, and Prometheus metrics.
- Non-root hardened container, loopback-only Compose example, CI, Dependabot, linting, type checking, dependency auditing, and comprehensive tests.
- Detailed methodology, security, SaaS strategy, API, and upstream-audit documentation.

### Changed

- Replaced the arbitrary `100 - 15 × warning count` score with transparent weighted categories.
- Replaced blocking `requests` usage with `httpx.AsyncClient`.
- Kept `/analyze`, `/quick-score`, and `/metadata` as compatibility routes while correcting their documentation/implementation mismatch.
- Treats `alt=""` as a possible valid decorative-image choice instead of always counting it as missing.
- Treats title/description character counts as preview heuristics, not hard search-engine limits.

### Removed

- Unbounded global dictionary cache.
- Full word-density map for every token.
- Claims that a plain HTML fetch measures LCP, CLS, or other browser Core Web Vitals.
