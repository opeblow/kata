# Privacy

Kata is local software. It has no servers, no accounts, no telemetry and no analytics.

## What Kata stores

Kata keeps one SQLite database in your Hermes profile at `<HERMES_HOME>/plugin-data/kata/kata.db`.

| Data | Why | Retention |
|---|---|---|
| Tool calls: tool name, arguments, result (first 64 KB), status, duration, conversation, session and turn identifiers | To detect routines and validate them | `retention_days`, default 30 |
| Model request timing: start time, duration, total token count per request | To measure the overhead a kata removes | Same as above |
| Katas: specs, versions, validation reports | So you can review, approve and roll back | Until you delete the database |
| Runs: per-step status, timing and a 400-character preview of each result | Traceability | Until you delete the database |
| Audit log: action, actor, time | Accountability for every decision | Until you delete the database |

Kata does not store prompts, model responses or message text.

## Redaction

Before anything is written, values under secret-named keys (for example `password`, `token`, `api_key`) and values
that match common credential formats (API keys, bearer tokens, JWTs, private keys, cloud access keys) are replaced
with `[REDACTED]`. A routine that would need a redacted value is never compiled.

## What leaves your machine

Nothing. Kata makes no network requests. The tools a kata runs are your existing Hermes tools, and they behave exactly as
they do when the agent calls them directly.

## Your controls

- Pause recording: set `plugins.entries.kata.settings.record: false`.
- Shorten retention: set `retention_days`.
- Delete everything: disable the plugin and remove `<HERMES_HOME>/plugin-data/kata/`.
- Uninstall: `hermes plugins remove kata`.
