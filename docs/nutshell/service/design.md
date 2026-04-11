# Service — Design

The service layer is the **single authorized interface** for reading or mutating session IPC/runtime state.

## Responsibilities

- Expose pure Python functions with no FastAPI or argparse dependency
- Both `ui/web/app.py` and `ui/cli/main.py` should be thin adapters calling service functions
- Encapsulate all IPC, status, and params access

## Design Rule

No UI code should import `nutshell.runtime.ipc`, `session_status`, `session_params`, or `bridge` directly. All access goes through service modules.
