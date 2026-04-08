# `tests/porter_system/`

Centralized porter-managed pytest coverage for subsystem structure, layout, README contracts, and tree-level behavior.

## Naming Rule

Each file uses:

```text
test_<component>_<version>_<topic>.py
```

Examples:

- `test_session_engine_v1_3_77_task_cards.py`
- `test_porter_system_v1_3_77_full_system.py`

This keeps porter-managed suites visually separate from feature-local pytest files that developers add during implementation.

## How To Use It

```bash
pytest tests/porter_system -q
pytest tests/porter_system/test_session_engine_v1_3_77_* -q
pytest tests/porter_system/test_porter_system_v1_3_77_* -q
```

- The first command runs the whole porter-managed suite.
- The second runs one subsystem.
- The third runs porter-level global/layout checks.
