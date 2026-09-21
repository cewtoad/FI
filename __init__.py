"""F1 TR (Telemetry Receiver) - minimal single-process skeleton.

Extracted from pits-n-giggles (MIT). Only the UDP receive + packet parse chain
and a handful of analysis helpers are kept. Everything else (launcher, backend
multi-process, HUD, web, IPC, MQTT...) was dropped.

Pipeline:
    F1 game UDP -> UdpTransport -> PacketParserFactory -> TelemetryState
                                                        -> analyzers
                                                        -> JSON snapshot
"""

__version__ = "0.0.1"
