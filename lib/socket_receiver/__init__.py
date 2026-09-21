"""Minimal socket receiver package (extracted from pits-n-giggles).

Only base + UDP transports are kept. The original IPC and TCP transports were
removed to avoid pulling in the ZeroMQ IPC subsystem.
"""

from .base_receiver import TelemetryTransport
from .udp_receiver import UdpTransport

__all__ = [
    "TelemetryTransport",
    "UdpTransport",
]
