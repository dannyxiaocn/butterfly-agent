# Terminal Executor — Implementation

## Files

| File | Purpose |
|------|---------|
| `bash_terminal.py` | Built-in `bash` tool with subprocess and PTY modes |
| `shell_terminal.py` | Executor for session/agent tools backed by `.sh` scripts |

## Usage

- **bash**: ad hoc shell commands, runs from session directory
- **`.json + .sh` pairs**: reusable session tools in `core/tools/`

Both run from the session directory by default when loaded through `Session`.
