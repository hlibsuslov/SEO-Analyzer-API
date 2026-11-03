# Contributing

Contributions should keep the API explainable, safe for caller-controlled public URLs, and honest about what static evidence can establish.

## Development setup

Python 3.11–3.15 is supported. Use a current patch release.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Run the complete local gate before opening a pull request:

```bash
ruff check .
ruff format --check .
mypy seo_analyzer
pytest --cov --cov-report=term-missing
pip-audit
python -m build
```

`make check` runs all six checks above. The CI matrix validates every Python minor
version from 3.11 through 3.15. Separate jobs verify wheel installation outside the
source tree, required license files, and health/readiness of the hardened container.
Resource warnings fail tests so unclosed SQLite connections cannot silently regress.

## Change expectations

- Add tests for fixes and new behavior, including failure and budget boundaries.
- Preserve SSRF validation for every request and redirect. New network providers need an explicit threat model.
- Keep evidence arrays and external work bounded.
- Give new findings a stable issue code, category, severity, evidence, remediation, confidence and appropriate tests.
- Do not mix conversion heuristics into core SEO weights.
- Avoid claims about rankings, traffic, rich-result eligibility or Core Web Vitals without the necessary source data.
- Update OpenAPI-facing models, README, methodology and changelog when contracts or scoring change.
- Increment `methodology_version` when a scoring change alters historical comparability.
- Preserve legacy routes unless the change includes a documented major-version migration.

## Pull requests

Create a focused branch, explain the user-visible result and risk, and include the checks you ran. Link relevant official specifications or search-engine documentation for policy-sensitive behavior. Never commit `.env`, API keys, target-site data or other secrets.

Security vulnerabilities should follow [SECURITY.md](SECURITY.md), not a public issue. All contributors must follow the [Code of Conduct](CODE_OF_CONDUCT.md).
