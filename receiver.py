"""Single-process UDP receiver for F1 telemetry.

Replaces pits-n-giggles' AsyncF1TelemetryManager + backend multi-process stack
with a small asyncio loop that drives the parser factory directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional, Set

from lib.f1_types import F1PacketType
from lib.socket_receiver import TelemetryTransport, UdpTransport
from lib.telemetry_manager import PacketParserFactory, SessionFrameGate

from state import TelemetryState

DEFAULT_PORT = 20777

# We receive and parse EVERY packet type, because the app supports two modes
# (time-trial and race) and the mode switch is a *reading* concern handled by
# the summariser/UI, not a receive-time filter. Dropping packets at receive
# time would make mode switching impossible without restarting.
PACKETS_ALL: Set[F1PacketType] = set(F1PacketType)

# Named subsets, kept for reference / future per-mode parsing if we ever want
# to save CPU. Currently the receiver uses PACKETS_ALL.
PACKETS_TIME_TRIAL: Set[F1PacketType] = {
    F1PacketType.SESSION,
    F1PacketType.LAP_DATA,
    F1PacketType.CAR_TELEMETRY,
    F1PacketType.CAR_STATUS,
    F1PacketType.CAR_DAMAGE,
    F1PacketType.CAR_TELEMETRY_2,
    F1PacketType.SESSION_HISTORY,
    F1PacketType.TIME_TRIAL,
    F1PacketType.MOTION,
    F1PacketType.MOTION_EX,
    F1PacketType.CAR_SETUPS,
}

PACKETS_RACE: Set[F1PacketType] = PACKETS_TIME_TRIAL | {
    F1PacketType.EVENT,
    F1PacketType.PARTICIPANTS,
    F1PacketType.LAP_POSITIONS,
    F1PacketType.TYRE_SETS,
    F1PacketType.FINAL_CLASSIFICATION,
    F1PacketType.LOBBY_INFO,
}


class TelemetryReceiver:
    """Owns the UDP transport, parser factory and telemetry state."""

    def __init__(
        self,
        state: TelemetryState,
        port: int = DEFAULT_PORT,
        bind_ip: str = "127.0.0.1",
        interested: Optional[Set[F1PacketType]] = None,
        logger: Optional[logging.Logger] = None,
        on_packet: Optional[Callable[[object], None]] = None,
    ) -> None:
        self.state = state
        self.port = port
        self.bind_ip = bind_ip
        self.logger = logger or logging.getLogger("f1_tr.receiver")
        self.on_packet = on_packet

        self.factory = PacketParserFactory(
            interested or PACKETS_ALL, self.logger
        )
        self.transport: TelemetryTransport = UdpTransport(
            port, bind_ip, buffer_size=16384
        )
        self.frame_gate = SessionFrameGate(enabled=True)
        self.frames = 0
        self.dropped_unparsed = 0
        self.dropped_gate = 0
        self.drop_reasons: dict = {}

    async def run(self) -> None:
        """Receive and parse packets until cancelled."""

        @self.transport.on_packet
        async def _handle(raw_packet: bytes) -> None:
            self._handle_raw(raw_packet)

        self.logger.info("Listening on %s:%s", self.bind_ip, self.port)
        try:
            await self.transport.run()
        except asyncio.CancelledError:
            await self.transport.close()
            raise

    def _handle_raw(self, raw_packet: bytes) -> None:
        # The factory raises (instead of returning None) for a few hard guards
        # such as unsupported packet formats. One malformed / old-format
        # packet must never kill the receive loop, so catch everything here,
        # count it and move on.
        try:
            packet = self.factory.parse(raw_packet)
        except Exception as e:  # noqa: BLE001 - intentionally broad
            self.dropped_unparsed += 1
            reason = f"parse-exception: {type(e).__name__}"
            self.drop_reasons[reason] = self.drop_reasons.get(reason, 0) + 1
            self.logger.warning("packet parse raised %s: %r", type(e).__name__, e)
            return
        if packet is None:
            self.dropped_unparsed += 1
            reason = self.factory.last_failure_reason or "unknown"
            self.drop_reasons[reason] = self.drop_reasons.get(reason, 0) + 1
            return
        if self.frame_gate.should_drop(packet):
            self.dropped_gate += 1
            reason = self.frame_gate.last_drop_reason or "gate"
            self.drop_reasons[reason] = self.drop_reasons.get(reason, 0) + 1
            return
        self.frames += 1
        self.state.process(packet)
        if self.on_packet is not None:
            self.on_packet(packet)

    def stats(self) -> dict:
        return {
            "accepted": self.frames,
            "dropped_unparsed": self.dropped_unparsed,
            "dropped_gate": self.dropped_gate,
            "drop_reasons": dict(self.drop_reasons),
        }
