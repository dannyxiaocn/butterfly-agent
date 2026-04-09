# `ui/web`

The Web frontend for Nutshell. It serves a monitoring UI and a small HTTP API over the same file-backed session model used by the CLI.

## What This Part Is

- `app.py`: FastAPI app, routes, and SSE stream.
- `sessions.py`: helper functions for session metadata and initialization.
- `weixin.py`: optional WeChat bridge.
- `index.html`: browser UI.

## How To Use It

```bash
nutshell web
```

Key endpoints:

- `GET /api/sessions`
- `POST /api/sessions`
- `POST /api/sessions/{id}/messages`
- `GET /api/sessions/{id}/events`
- `GET /api/sessions/{id}/history`
- `GET /api/sessions/{id}/tasks`: returns task cards from `core/tasks/` as `{"cards": [...]}` and migrates legacy `core/tasks.md` on first read
- `PUT /api/sessions/{id}/tasks`: creates or updates a named task card; when updating an existing card, schedule metadata is preserved unless explicitly overridden
- `GET /api/sessions/{id}/config`
- `PUT /api/sessions/{id}/config`

## How It Contributes To The Whole System

This directory gives operators a live, streaming view of session activity without introducing a second state model. Everything still comes from the on-disk session files.
