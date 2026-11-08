# Changelog

All notable changes to Kata are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security

- A step's arguments are classified again after rendering, just before dispatch. A step whose real arguments are
  riskier than the effect it was approved with is blocked and never sent or retried.
- Values spliced into a terminal command (kata inputs and earlier steps' results) are shell-quoted outside quotes and
  refused inside quotes when they could close or escape them. Values that would start a new word with `-` are
  refused, and the program name must be fixed text.
- The command classifier treats newlines and NUL as compound commands, so `git log x` followed by a second line is no
  longer read-only.

### Changed

- The database always lives in Hermes's plugin storage. The `KATA_DB_PATH` override and the `~/.hermes` fallback are
  gone; tests and dev tools pass a store explicitly (`install(ctx, store)`, or `use_store(path)` in the dashboard API),
  and the dashboard preview takes `--db PATH`.

## [0.1.0] - 2026-11-07

First public release, submitted to the Hermes Agent Plugin Hackathon.

### Added

- Records the tool calls and model requests of every Hermes session to a local SQLite file, with secret redaction and
  30-day retention.
- Mines finished conversations for multi-step routines the agent repeats, and infers inputs, constants and the data
  flow between steps.
- Twelve-check offline validation for every proposal: exact replay of recorded runs, held-out fit, injected failures
  and timeouts, schema checks, secret scan, tool availability and side-effect classification.
- Human approval through `/kata`, `hermes kata` or the Kata dashboard tab. Nothing is registered without it.
- Deterministic executor that runs approved steps through `ctx.dispatch_tool` with no model calls between them, stops
  at the first failure, never retries side-effecting steps and traces every run.
- Drift detection: a kata whose underlying tool schema changes is marked stale and re-proposed for review.
- Measured savings per kata (tool calls, model round-trips, tokens), shown in `/kata stats` and the dashboard.
- Website, documentation page and dashboard in one design system.

### Fixed during validation in real Hermes (v0.23.0)

- Paths written as `src\file.py` and `src/file.py` are matched, so data flow is found on Windows.
- Regex escapes in recorded search patterns are treated as fixed structure, so the learned input is the plain value.
- The database opens lazily and is released after loading, so `hermes plugins doctor` passes on Windows.
- Tool checks are deferred to the agent process when approving from the CLI or dashboard.
- Only finished turns are mined, so a routine still in progress is never proposed as a fragment.
- Prompt text is built from plain strings, so Hermes's install-time security scan reports **safe**.
- Input schemas show the values recorded in past runs, so the agent passes `"alice"` instead of a whole regex.
- The routines prompt tells the agent to load a kata with `tool_search` when Hermes defers plugin tools.

[0.1.0]: https://github.com/opeblow/kata/releases/tag/v0.1.0
