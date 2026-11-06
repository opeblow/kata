# Security Policy

Kata runs inside your Hermes agent and can execute tools on your behalf, so we treat security reports as the
highest priority.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Reporting a vulnerability

Do not open a public issue. Use GitHub's private vulnerability reporting on this repository
(**Security > Report a vulnerability**). Include what you found, how to reproduce it, and the impact you expect.

We aim to acknowledge reports within 72 hours and to ship a fix or mitigation for confirmed high-severity issues within
14 days. We will credit you in the release notes unless you ask us not to.

## Threat model

Kata's job is to turn observed tool calls into tools. The security properties it maintains:

| Property | How it is enforced |
|---|---|
| No tool without consent | Only `KataService.approve` can set a kata active, and it re-runs validation first. Every transition is audited. |
| No silent side effects | Steps are classified per call. Katas with external or unknown effects request approval through `pre_tool_call` on every invocation, with a unique rule key so remembered approvals cannot be reused. |
| No repeated side effects | Only read-only steps are retried. A timed-out step is never re-issued. |
| Least privilege | A kata dispatches only the tools listed in its own approved steps, through `ctx.dispatch_tool`. |
| No embedded credentials | Arguments and results are redacted before storage. A routine whose constant arguments contain a secret or a redacted value is rejected. |
| No stale behaviour | Tool schemas are fingerprinted. A changed or missing tool marks the kata stale and unregisters it before it can run. |
| No core patching | Kata uses only documented plugin interfaces. It does not modify Hermes internals or other plugins. |
| No network | Kata makes no network requests. The dashboard API is served by Hermes behind its authentication gate. |

## Known limits

- Side-effect classification of shell commands is heuristic. It is conservative: anything unrecognised is treated as
  external and requires approval per call. If you find a command classified as read that can change state, please
  report it.
- Redaction is pattern-based. It masks common credential formats and secret-named keys, but cannot recognise every
  possible secret. Kata stores data only in your local Hermes profile.
- Kata relies on the host's approval prompt. Under unattended runs, Hermes's approval policy decides.

## Disclosure in the catalog

As required by the Hermes plugin catalog rules, risky behaviour is disclosed in the catalog entry and in this
repository: Kata records tool arguments and results locally, registers tools that you approve, and executes their
steps through Hermes's dispatcher.
