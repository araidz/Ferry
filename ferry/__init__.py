"""ferry — a terminal VPN hopper over the free VPN Gate relay list."""

from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"

# App-wide state dir (matches Trawl's ~/Library/Application Support/<App> layout).
STATE_DIR = Path.home() / "Library" / "Application Support" / "Ferry"
