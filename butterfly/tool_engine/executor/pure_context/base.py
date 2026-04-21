"""Pure-context trait shared by pty-backed tools.

`strip_ansi` handles CSI / OSC / a few bare ESC sequences plus carriage
returns, covering what `ls --color`, colored shell prompts, bracketed-paste
toggles, and cursor hide/show typically emit. Rare terminal-specific escapes
may leak through; the model tolerates the occasional stray byte.
"""
from __future__ import annotations

import re

# CSI: ESC [ <param-bytes> <intermediate-bytes> <final-byte>
#   param 0x30-0x3F  (digits + :;<=>?)
#   inter 0x20-0x2F
#   final 0x40-0x7E
_CSI = re.compile(r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")
# OSC: ESC ] ... (BEL | ESC \)
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Bare two-byte ESC sequences (cursor save/restore, charset select, etc.)
_ESC_SHORT = re.compile(r"\x1b[=>NOPQ^_\\()]")


def strip_ansi(text: str) -> str:
    """Return `text` with ANSI control sequences removed.

    Also normalises CRLF → LF and drops bare CR (line-display consumers
    render those as literal overwrites, which is never what we want for a
    log-style Terminal panel).
    """
    text = _CSI.sub("", text)
    text = _OSC.sub("", text)
    text = _ESC_SHORT.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "")
    return text
