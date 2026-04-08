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

## How It Contributes To The Whole System

This directory gives operators a live, streaming view of session activity without introducing a second state model. Everything still comes from the on-disk session files.

