"""ferry — a terminal VPN hopper over free VPN relay lists."""

from __future__ import annotations

from pathlib import Path

__version__ = "0.3.0"

# App-wide state dir (matches Trawl's ~/Library/Application Support/<App> layout).
STATE_DIR = Path.home() / "Library" / "Application Support" / "Ferry"


def get_providers():
    """Lazy-loaded list of available providers."""
    from .vpngate import VPNGate
    from .vpnbook import VPNBook
    return [VPNGate(), VPNBook()]
