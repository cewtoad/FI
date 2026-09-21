"""Minimal telemetry_manager package (extracted from pits-n-giggles).

Only the parser factory, frame gate and exceptions are kept. The async
AsyncF1TelemetryManager was removed since our single-process receiver drives
the factory directly.
"""

from .exceptions import UnsupportedPacketFormat, UnsupportedPacketType
from .factory import PacketParserFactory, telemetry_transport_factory
from .frame_gate import SessionFrameGate

__all__ = [
    "PacketParserFactory",
    "telemetry_transport_factory",
    "SessionFrameGate",
    "UnsupportedPacketFormat",
    "UnsupportedPacketType",
]
