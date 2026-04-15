"""Default web_fetch provider: plain HTTP GET via `httpx` + article extraction.

Extraction strategy (first available wins):
  1. trafilatura (best, optional dep)
  2. BeautifulSoup4 (good, optional dep)
  3. Naive regex strip of `<[^>]+>` tags + whitespace collapse (always works)

Security:
  * Only `http`/`https` URLs accepted.
  * Hostname is resolved and rejected if any resolution points at a loopback,
    link-local, private (RFC1918 / ULA), or reserved address — blocks SSRF to
    metadata endpoints (169.254.169.254), internal services, and containers.
  * Response body is streamed with a hard byte ceiling; huge pages abort mid-
    download rather than exhausting memory.

`httpx` is a transitive dependency of `anthropic>=0.40`, so no new hard dep is
added to `pyproject.toml`.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from typing import Optional
from urllib.parse import urlparse


_USER_AGENT = "ButterflyAgent/2.0.5 (+https://github.com/dannyxiaocn/butterfly-agent)"
_TIMEOUT_SECONDS = 30
_DEFAULT_MAX_CHARS = 20000
# Hard cap on the raw bytes downloaded per fetch. Prevents memory exhaustion
# on pathological pages; extracted body is further truncated to max_chars.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTS = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
})


# ── Security: SSRF guard ──────────────────────────────────────────────────────


def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject loopback / link-local / private / reserved / unspecified."""
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def _allow_loopback() -> bool:
    """Escape hatch for test environments that spin up local httpd fixtures.

    When set to `1` / `true` / `yes` (case-insensitive), the SSRF guard still
    rejects metadata/private/reserved ranges but permits loopback + link-local.
    **Do not set this in production** — it disables the core localhost block.
    """
    val = os.environ.get("BUTTERFLY_WEB_FETCH_ALLOW_LOOPBACK", "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _validate_url(url: str) -> tuple[bool, str]:
    """Return (ok, error_message). Resolves hostname and rejects SSRF targets."""
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"Error: could not parse URL {url!r}: {exc}"

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False, (
            f"Error: web_fetch only allows http/https URLs; got scheme "
            f"{parsed.scheme!r} for {url}"
        )

    host = parsed.hostname
    if not host:
        return False, f"Error: URL {url!r} has no hostname"

    # Test-only escape hatch: permit loopback + link-local when explicitly
    # enabled via env. Private / reserved / multicast / unspecified remain
    # rejected even in this mode.
    loopback_ok = _allow_loopback()

    if host.lower() in _BLOCKED_HOSTS and not loopback_ok:
        return False, (
            f"Error: refusing to fetch from {host!r} — localhost is blocked "
            "(SSRF guard)"
        )

    def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, str]:
        if loopback_ok and (ip.is_loopback or ip.is_link_local):
            return True, ""
        if _is_disallowed_ip(ip):
            return False, (
                f"Error: refusing to fetch from {ip} — address is "
                "loopback/link-local/private/reserved (SSRF guard)"
            )
        return True, ""

    # If the host is already an IP literal, validate it directly.
    try:
        literal = ipaddress.ip_address(host)
        ok, err = _check_ip(literal)
        return (True, "") if ok else (False, err)
    except ValueError:
        pass  # Not an IP literal; fall through to DNS resolution.

    # Resolve all A/AAAA answers; if ANY is disallowed, refuse.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"Error: could not resolve {host!r}: {exc}"

    for _family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        # IPv6 scoped addresses come with a zone-id suffix; strip it.
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        ok, err = _check_ip(ip)
        if not ok:
            return False, err
    return True, ""


# ── Extraction helpers ────────────────────────────────────────────────────────


def _extract_title_naive(html: str) -> Optional[str]:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title or None


def _extract_with_trafilatura(html: str, url: str) -> tuple[Optional[str], Optional[str]]:
    try:
        import trafilatura  # type: ignore
    except Exception:
        return None, None
    try:
        body = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
    except Exception:
        body = None
    title: Optional[str] = None
    try:
        meta = trafilatura.extract_metadata(html)
        if meta is not None:
            title = getattr(meta, "title", None)
    except Exception:
        title = None
    return title, body


def _extract_with_bs4(html: str) -> tuple[Optional[str], Optional[str]]:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        return None, None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None, None

    title: Optional[str] = None
    if soup.title and soup.title.string:
        title = re.sub(r"\s+", " ", soup.title.string).strip() or None

    # Drop obvious non-content nodes.
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()

    # Prefer <article> / <main>, then <body>.
    root = soup.find("article") or soup.find("main") or soup.body or soup
    text = root.get_text(separator="\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return title, (text or None)


def _extract_naive(html: str) -> tuple[Optional[str], str]:
    title = _extract_title_naive(html)
    # Strip script/style blocks wholesale first.
    cleaned = re.sub(
        r"<(script|style|noscript)[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return title, cleaned


def _extract(html: str, url: str) -> tuple[Optional[str], str]:
    # Always prefer the HTML `<title>` tag for the title — it's deterministic
    # across environments. Trafilatura's metadata.title sometimes picks an
    # <h1> instead, which differs between test runs depending on whether
    # trafilatura is installed. Fall back to extractor metadata if no <title>.
    html_title = _extract_title_naive(html)
    _traf_title, body = _extract_with_trafilatura(html, url)
    if body:
        return (html_title or _traf_title), body
    _bs4_title, body = _extract_with_bs4(html)
    if body:
        return (html_title or _bs4_title), body
    _naive_title, body = _extract_naive(html)
    return (html_title or _naive_title), body


# ── Fetch ─────────────────────────────────────────────────────────────────────


class HttpxFetcher:
    """Fetch a URL via httpx and return extracted main text."""

    def __init__(
        self,
        timeout: float = _TIMEOUT_SECONDS,
        user_agent: str = _USER_AGENT,
        max_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        self._timeout = timeout
        self._user_agent = user_agent
        self._max_bytes = max_bytes

    async def execute(self, url: str, max_chars: int | float = _DEFAULT_MAX_CHARS) -> str:
        try:
            import httpx  # type: ignore
        except Exception as exc:
            return f"Error: httpx is required for web_fetch but not installed: {exc}"

        ok, err = _validate_url(url)
        if not ok:
            return err

        try:
            max_chars_int = int(max_chars)
        except Exception:
            max_chars_int = _DEFAULT_MAX_CHARS
        if max_chars_int <= 0:
            max_chars_int = _DEFAULT_MAX_CHARS

        headers = {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        }

        async def _do_fetch() -> tuple[int, bytes, bool]:
            """Stream the response body with a hard byte ceiling.
            Returns (status_code, bytes_read, capped)."""
            chunks: list[bytes] = []
            total = 0
            capped = False
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self._timeout,
                headers=headers,
            ) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        return resp.status_code, b"", False
                    async for chunk in resp.aiter_bytes():
                        if total + len(chunk) > self._max_bytes:
                            chunks.append(chunk[: self._max_bytes - total])
                            capped = True
                            break
                        chunks.append(chunk)
                        total += len(chunk)
            return resp.status_code, b"".join(chunks), capped

        try:
            status, raw, capped = await asyncio.wait_for(
                _do_fetch(), timeout=self._timeout + 5
            )
        except asyncio.TimeoutError:
            return f"Error: Timed out fetching {url}"
        except Exception as exc:
            exc_name = type(exc).__name__
            if "Timeout" in exc_name:
                return f"Error: Timed out fetching {url}"
            reason = str(exc) or exc_name
            return f"Error: Could not reach {url}: {reason}"

        if status >= 400:
            return f"Error: HTTP {status} fetching {url}"

        byte_len = len(raw)
        html = raw.decode("utf-8", errors="replace")

        title, body = _extract(html, url)
        body = body or ""
        truncated = len(body) > max_chars_int
        if truncated:
            body = body[:max_chars_int]

        title_line = f"# {title}" if title else "# (no title)"
        if capped:
            size_note = (
                f"[fetched {byte_len} bytes (capped at {self._max_bytes}), "
                f"body truncated at {max_chars_int} chars]"
                if truncated
                else f"[fetched {byte_len} bytes (capped at {self._max_bytes})]"
            )
        else:
            size_note = (
                f"[fetched {byte_len} bytes, body truncated at {max_chars_int} chars]"
                if truncated
                else f"[fetched {byte_len} bytes]"
            )
        parts = [
            title_line,
            f"<source url: {url}>",
            "",
            body.strip(),
            "",
            size_note,
        ]
        return "\n".join(parts)


async def _httpx_fetch(url: str, max_chars: int | float = _DEFAULT_MAX_CHARS) -> str:
    """Module-level entry point used by the registry."""
    return await HttpxFetcher().execute(url=url, max_chars=max_chars)
