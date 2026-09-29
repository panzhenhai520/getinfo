"""Runtime stdout/stderr encoding guard for Windows consoles."""

import sys


def _reconfigure_stream(stream):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_reconfigure_stream(sys.stdout)
_reconfigure_stream(sys.stderr)
