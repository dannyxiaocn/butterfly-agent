"""Regression coverage for PR #37 / v2.0.22 display_name plumbing.

Covers:
  * POST /api/sessions ignores body "id" and auto-generates session_id.
  * display_name round-trips through manifest + get_session + list_sessions.
  * Blank / whitespace display_name → no manifest field, no leaked value.
  * 40-char cap is applied consistently — the POST response must agree with
    what actually ends up in manifest.json (regression for cubic's P2 finding:
    the web-layer strip() path did not mirror init_session's truncate, so a
    50-char input returned 50 chars in the POST body and 40 chars in
    get_session on the next read — a silent drift between the create reply
    and every subsequent read of the same session).
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from ui.web.app import create_app


_AGENT_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "agenthub"


def _app_with_tmp(root: Path):
    return create_app(root / "sessions", root / "_sessions", _AGENT_ROOT)


class DisplayNameCreationTests(unittest.TestCase):
    def test_post_sessions_rejects_body_id_with_400(self) -> None:
        # PR #37 review finding #4: the old contract accepted an ``id`` field
        # in the body (and validated it). The new contract makes session_id
        # strictly server-generated. Silently dropping the caller's ``id``
        # would mask bugs in client code that still assumes the old shape,
        # so we surface the contract change as an explicit 400.
        with TemporaryDirectory() as td:
            root = Path(td)
            with TestClient(_app_with_tmp(root)) as client:
                r = client.post("/api/sessions", json={
                    "id": "client-provided-should-be-ignored",
                    "agent": "agent",
                })
                self.assertEqual(r.status_code, 400)
                self.assertIn("id", r.json().get("detail", "").lower())
                # Nothing should have been written to disk on a rejected request.
                self.assertEqual(list((root / "_sessions").glob("*")), [])

    def test_display_name_round_trip(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            with TestClient(_app_with_tmp(root)) as client:
                r = client.post("/api/sessions", json={
                    "agent": "agent",
                    "display_name": "audit auth flow",
                })
                self.assertEqual(r.status_code, 200)
                sid = r.json()["id"]
                manifest = json.loads((root / "_sessions" / sid / "manifest.json").read_text())
                self.assertEqual(manifest.get("display_name"), "audit auth flow")
                # list_sessions surfaces the same value
                listed = client.get("/api/sessions").json()
                match = next(s for s in listed if s["id"] == sid)
                self.assertEqual(match["display_name"], "audit auth flow")

    def test_blank_display_name_omitted_from_manifest(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            with TestClient(_app_with_tmp(root)) as client:
                r = client.post("/api/sessions", json={
                    "agent": "agent",
                    "display_name": "   ",
                })
                self.assertEqual(r.status_code, 200)
                sid = r.json()["id"]
                self.assertIsNone(r.json()["display_name"])
                manifest = json.loads((root / "_sessions" / sid / "manifest.json").read_text())
                self.assertNotIn("display_name", manifest)

    def test_post_response_matches_persisted_display_name_for_long_input(self) -> None:
        """Regression: cubic P2 finding on PR #37.

        ``init_session`` caps display_name at 40 chars. The web layer used
        to trim but not cap, so a 50-char POST returned 50 chars in the
        reply while the manifest (and every subsequent GET) carried 40.
        The POST response must agree with what was actually persisted.
        """
        long_name = "X" * 50
        with TemporaryDirectory() as td:
            root = Path(td)
            with TestClient(_app_with_tmp(root)) as client:
                r = client.post("/api/sessions", json={
                    "agent": "agent",
                    "display_name": long_name,
                })
                self.assertEqual(r.status_code, 200)
                sid = r.json()["id"]
                manifest = json.loads((root / "_sessions" / sid / "manifest.json").read_text())
                persisted = manifest.get("display_name")
                self.assertEqual(len(persisted), 40)
                self.assertEqual(
                    r.json().get("display_name"),
                    persisted,
                    "POST response must mirror what was persisted",
                )


if __name__ == "__main__":
    unittest.main()
