"""TelemetrySource adapter: pluggable packet feed into TelemetryState.

The default production path is UDP (``UdpTelemetrySource`` / the existing
``TelemetryReceiver``). This ABC exists so a future replay file, forwarder, or
LAN mirror can plug in without rewriting ``app.build_app``.

Field coverage is limited to what the official F1 UDP telemetry publishes —
see PROGRAMMER.md. There is intentionally **no** memory-read / injection
source here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class TelemetrySource(ABC):
    """Push-based telemetry feed that eventually mutates a TelemetryState."""

    @abstractmethod
    async def run(self) -> None:
        """Receive until cancelled."""

    @abstractmethod
    async def close(self) -> None:
        """Release OS resources."""

    def stats(self) -> Dict[str, Any]:
        return {}


class UdpTelemetrySource(TelemetrySource):
    """Default source: thin adapter over ``receiver.TelemetryReceiver``."""

    def __init__(self, receiver: Any) -> None:
        self._receiver = receiver

    @property
    def receiver(self) -> Any:
        return self._receiver

    async def run(self) -> None:
        await self._receiver.run()

    async def close(self) -> None:
        # TelemetryReceiver.run() closes transport on cancel; expose a
        # best-effort close for callers that hold the adapter directly.
        transport = getattr(self._receiver, "transport", None)
        if transport is not None and hasattr(transport, "close"):
            await transport.close()

    def stats(self) -> Dict[str, Any]:
        if hasattr(self._receiver, "stats"):
            return self._receiver.stats()
        return {}


def make_telemetry_source(kind: str = "udp", *, receiver: Any = None,
                          **kwargs: Any) -> TelemetrySource:
    """Factory for TelemetrySource implementations.

    Only ``udp`` is shipped. Unknown kinds raise — do not invent a silent
    memory/injection fallback.
    """
    kind = (kind or "udp").strip().lower()
    if kind == "udp":
        if receiver is None:
            from receiver import TelemetryReceiver
            receiver = TelemetryReceiver(**kwargs)
        return UdpTelemetrySource(receiver)
    raise ValueError(
        f"unknown telemetry source {kind!r}; only 'udp' is supported "
        "(official F1 UDP — no memory telemetry)"
    )
