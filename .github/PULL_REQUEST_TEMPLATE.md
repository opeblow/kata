## What changes

<!-- Behaviour before and after. Link the issue if there is one. -->

## Safety

- [ ] No tool can be registered or activated without an explicit human decision
- [ ] Side-effecting steps are still never retried, and still request approval per call
- [ ] Redaction and drift detection are unchanged, or the change is tested
- [ ] Only documented Hermes plugin interfaces are used

## Checks

- [ ] `ruff check .`
- [ ] `python -m pytest`
- [ ] `python scripts/demo.py`
- [ ] `hermes plugins validate ./kata` (if Hermes is installed)
- [ ] Screenshots updated for visual changes
