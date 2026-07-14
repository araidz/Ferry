"""Provider protocol: each source of servers implements this."""

from __future__ import annotations

from typing import Protocol

from .vpngate import Server


class Provider(Protocol):
    @property
    def name(self) -> str: ...

    def fetch(self, timeout: float = 20.0) -> list[Server]: ...

    def available(self) -> bool: ...
