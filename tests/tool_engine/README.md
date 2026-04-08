# `tests/tool_engine`

Documentation marker for tool-engine porter coverage.

## Current Role

- tool-engine pytest modules now live in `tests/porter_system/`
- file names use the `test_tool_engine_<version>_<topic>.py` convention
- this directory remains only as a topical README location

## How To Use It

```bash
pytest tests/porter_system/test_tool_engine_v1_3_77_* -q
```
