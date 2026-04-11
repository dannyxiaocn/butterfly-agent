# Tests — Implementation

## Structure

- `porter_system/`: centralized porter-managed pytest modules
- `runtime/`: documentation marker (tests live in porter_system/)
- `tool_engine/`: documentation marker (tests live in porter_system/)

## Usage

```bash
pytest tests/ -q                                          # All tests
pytest tests/porter_system -q                             # Porter suite
pytest tests/porter_system/test_session_engine_v1_3_77_* -q  # One subsystem
```
