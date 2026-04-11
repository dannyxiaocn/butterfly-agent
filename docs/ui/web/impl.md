# Web UI — Implementation

## Files

| File | Purpose |
|------|---------|
| `app.py` | FastAPI app, routes, SSE stream, HUD endpoint |
| `sessions.py` | Helper functions for session metadata and initialization |
| `weixin.py` | Optional WeChat bridge |
| `frontend/` | Vite + TypeScript SPA (`npm run build` → `dist/`) |

## Key Endpoints

- `GET /api/sessions` — list sessions
- `POST /api/sessions` — create session
- `POST /api/sessions/{id}/messages` — send message
- `GET /api/sessions/{id}/events` — SSE stream with byte-offset reconnect
- `GET /api/sessions/{id}/history` — display events with offset for SSE attach
- `GET/PUT /api/sessions/{id}/tasks` — task card CRUD
- `GET/PUT /api/sessions/{id}/config` — session config
- `GET /api/sessions/{id}/hud` — HUD bar data

## Frontend Architecture

TypeScript + Vite SPA:
- `main.ts`: session attach with monotonic version token
- `sse.ts`: `SSEConnection` with contextSince/eventsSince tracking for reconnect
- `components/chat.ts`: message batching, streaming bubble management
- `components/sidebar.ts`: persistent form state
