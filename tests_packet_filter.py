"""R5 regression: unconsumed packet types are dropped after a header parse.

PACKETS_CONSUMED (fed to the factory by run.py and webui.py) must cover
exactly the packet types TelemetryState._dispatch() has branches for:
  - a packet of an unconsumed type (MOTION) is dropped at the factory's
    "uninterested" check - dropped_unparsed+1, reason recorded, no payload
    parse, state untouched;
  - a consumed type (LAP_DATA) is still accepted;
  - a source-level check keeps PACKETS_CONSUMED in sync with _dispatch when
    new handlers are added or removed.

Run: py -3.12 tests_packet_filter.py
"""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap

from fake_data import PACKET_FORMAT, make_lap
from lib.f1_types import F1PacketType, PacketHeader
import receiver
import state
from receiver import TelemetryReceiver
from state import TelemetryState


def motion_raw_packet() -> bytes:
    """MOTION header + junk payload. The payload must never be parsed."""
    hdr = PacketHeader.from_values(
        packet_format=2026, game_year=26, game_major_version=1,
        game_minor_version=0, packet_version=1, packet_type=F1PacketType.MOTION,
        session_uid=9, session_time=0.0, frame_identifier=1,
        overall_frame_identifier=1, player_car_index=0,
        secondary_player_car_index=255)
    return hdr.to_bytes() + b"\x00" * 1300


def main():
    # 1) source-level sync: PACKETS_CONSUMED == the types _dispatch branches on
    src = inspect.getsource(state.TelemetryState._dispatch)
    src = textwrap.dedent(src)
    used = {
        node.attr for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "F1PacketType"
    }
    consumed = {p.name for p in receiver.PACKETS_CONSUMED}
    print("_dispatch handles:", sorted(used))
    print("PACKETS_CONSUMED:", sorted(consumed))
    assert used == consumed, (
        f"PACKETS_CONSUMED out of sync with TelemetryState._dispatch: "
        f"missing={sorted(used - consumed)} extra={sorted(consumed - used)}")
    assert F1PacketType.MOTION not in receiver.PACKETS_CONSUMED

    # 2) behaviour: unconsumed type dropped after header parse, loop alive
    st = TelemetryState()
    recv = TelemetryReceiver(st, port=0, bind_ip="127.0.0.1",
                             interested=receiver.PACKETS_CONSUMED)
    recv._handle_raw(motion_raw_packet())
    stats = recv.stats()
    print("after MOTION:", stats)
    assert recv.frames == 0, stats
    assert stats["dropped_unparsed"] == 1, stats
    assert any("Uninterested" in r for r in stats["drop_reasons"]), stats
    assert not st.packet_counts, st.packet_counts  # state never saw it

    # 3) behaviour: consumed type still accepted and processed
    recv._handle_raw(make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, 42, 1, 0.0,
                              0, 1000, 100.0, 100.0, 1, 1, 0))
    stats = recv.stats()
    print("after LAP_DATA:", stats)
    assert recv.frames == 1, stats
    assert stats["dropped_unparsed"] == 1, stats
    assert st.snapshot()["latest"].get("lap"), "LAP_DATA not processed"

    # 4) diagnostic tools keep the full set available
    from receiver import PACKETS_ALL
    assert F1PacketType.MOTION in PACKETS_ALL

    print("\nPACKET FILTER OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
