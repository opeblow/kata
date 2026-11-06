# Contributing to Kata

Thank you for helping. Kata executes tools on people's machines, so we hold changes to a high bar for correctness and
safety. This guide explains how to work on it.

## Ground rules

- Use only documented Hermes plugin interfaces. If you need something that is not on Hermes's public plugin API list,
  open an issue first.
- Nothing may register or activate a tool without an explicit human decision. Changes that weaken approval, retries,
  redaction or drift detection will not be merged without a design discussion.
- Every behaviour change ships with a test.
- Keep runtime dependencies at zero. Development dependencies are fine.

## Setup

```bash
git clone https://github.com/opeblow/kata && cd kata
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
python -m playwright install chromium          # only for screenshots
```

## Checks

```bash
ruff check .
python -m pytest
python scripts/demo.py
```

With Hermes installed, also run its own checks before opening a pull request:

```bash
hermes plugins validate ./kata --install-deps
hermes plugins doctor ./kata --ci
```

## Working on the dashboard

The tab is a plain IIFE in `kata/dashboard/dist/index.js` that uses React from the host SDK. There is no build step.
To see it with real data:

```bash
python scripts/demo.py --db /tmp/kata-demo.db
KATA_DB_PATH=/tmp/kata-demo.db python devtools/preview_server.py
```

Then open http://127.0.0.1:8765 (add `?theme=light` for the light theme). Visual changes need updated screenshots:
`python devtools/screenshot.py dashboard http://127.0.0.1:8765`.

The website in `site/` follows the design system in [docs/DESIGN-SYSTEM.md](docs/DESIGN-SYSTEM.md).

## Pull requests

1. Branch from `main`.
2. Keep the change focused and describe the behaviour before and after.
3. Fill in the pull request template, including the safety section.
4. CI must pass on all platforms.

## Commit messages

Use the imperative mood and explain why, for example: `Skip retries for timed-out steps so hung tools are never re-issued`.

## Releasing

1. Bump `version` in `kata/plugin.yaml`, `kata/__init__.py`, `kata/dashboard/manifest.json` and `pyproject.toml`.
2. Tag the release.
3. Open a pull request against `NousResearch/hermes-agent` that bumps the `sha` in `plugin-catalog/kata.yaml` to the
   new 40-character commit.
