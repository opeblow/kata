# Hermes Agent Plugin Hackathon: submission checklist

Deadline: **5 PM EST, November 11, 2026**. Judged on usefulness, reliability, polish and creativity, favouring plugins
that do more than a prompt or a skill could.

## 1. Verify against a real Hermes install

```bash
hermes plugins validate ./kata --install-deps   # same checks as catalog CI
hermes plugins doctor ./kata --ci
hermes plugins install ./kata && hermes plugins enable kata
```

Then repeat one real task in three new sessions (for example "find the TODO for alice in src and count the lines of that
file"), run `/kata mine`, `/kata review 1`, `/kata approve 1 as triage_todo`, and ask for the same task once more in a new
session.

## 2. Publish the repository

Push to `github.com/<you>/kata` (public). If your GitHub username is not `opeblow`, replace `opeblow` in `README.md`,
`kata/plugin.yaml`, `site/*.html`, `.github/ISSUE_TEMPLATE/config.yml`, `CONTRIBUTING.md` and `docs/catalog-entry.yaml`.

## 3. Catalog pull request

1. Copy `docs/catalog-entry.yaml` to `plugin-catalog/kata.yaml` in a fork of `NousResearch/hermes-agent`.
2. Replace `<SHA>` with the full 40-character commit hash you pushed.
3. Open the pull request: `plugin-catalog: add kata (Hermes Agent Plugin Hackathon entry)`.

## 4. Demo video (under 90 seconds), posted on X tagging @NousResearch

| Time | Show |
|---|---|
| 0:00 to 0:08 | The hero line: "Your agent repeats itself. Kata notices." |
| 0:08 to 0:25 | Three real Hermes sessions doing the same task, sped up. Tool calls visible. |
| 0:25 to 0:45 | `/kata review 1` or the dashboard tab: steps, data flow, side effects, 12 green checks. |
| 0:45 to 0:55 | Click **Approve and register**, naming it `triage_todo`. |
| 0:55 to 1:15 | New session, same request: one `kata_triage_todo` call, trace with three steps. |
| 1:15 to 1:28 | Dashboard stats: tool calls and round-trips saved. Close on "Do it three times. Approve it once." |

## 5. Submit the form

Submit the pull request link and the X post link through the form linked in the hackathon announcement.
