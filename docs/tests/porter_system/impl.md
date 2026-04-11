# Porter System Tests — Implementation

## Usage

```bash
pytest tests/porter_system -q                                    # Full suite
pytest tests/porter_system/test_session_engine_v1_3_77_* -q      # One subsystem
pytest tests/porter_system/test_porter_system_v1_3_77_* -q       # Global checks
```

## Coverage Areas

- Session engine: task cards, IPC lifecycle
- Runtime: hook events, daemon loop
- Tool engine: loader, executors
- Porter system: full system layout checks
- Service layer: adapter-parity tests
