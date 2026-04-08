# nutshell/tool_engine/executor/terminal

This directory contains shell execution backends.

## What It Is

- `bash_terminal.py`: the built-in `bash` tool with subprocess and PTY modes
- `shell_terminal.py`: executor for session/entity tools backed by `.sh` scripts

## How To Use It

- Use `bash` for ad hoc shell commands.
- Use `.json + .sh` pairs in `core/tools/` when you want a reusable session tool.

Both run from the session directory by default when loaded through `Session`.

## How It Fits

These executors are what make the filesystem-as-everything model practical: the agent can inspect, edit, and operate on its own workspace through normal shell commands and script-backed tools.
