# UI — Design

The UI directory exposes the runtime to humans and external channels. It is intentionally thin: translates user actions into file operations and runtime calls.

## Design Rule

UI layers are pure adapters. All business logic lives in `nutshell/service/`. UI code should not import `nutshell.runtime.ipc`, `session_status`, `session_params`, or `bridge` directly.
