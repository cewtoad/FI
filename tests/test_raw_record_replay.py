"""T1.6: raw UDP recording + replay round-trip.

Records self-made packets to a .f1rec file, replays them through a receiver's
``_handle_raw``, and asserts the resulting state matches processing the same
packets directly.
"""

from __future__ import annotations

import io

from fake_data import PACKET_FORMAT, make_lap, make_status
from lib.f1_types import F1PacketType
from receiver import PACKETS_CONSUMED, TelemetryReceiver
from state import TelemetryState
from tools.replay import read_records, replay_into
from tools.udp_record import write_record


def _packets() -> list:
    pkts = []
    uid = 7
    frame = 1
    t = 0.0
    for lap_no in range(1, 4):
        for step in range(5):
            dist = 5000 * step / 5
            cur = int(83000 * step / 5)
            fuel = 50 - (lap_no - 1) * 1.5 - 1.5 * step / 5
            pkts.append(make_lap(PACKET_FORMAT, F1PacketType.LAP_DATA, uid, frame,
                                 t, (83000 if lap_no > 1 else 0), cur, dist,
                                 5000 * (lap_no - 1) + dist, lap_no, 1, 0))
            pkts.append(make_status(PACKET_FORMAT, uid, frame, t, fuel, 3.0, lap_no))
            frame += 1
            t += 0.8
    return pkts


def _record_bytes(tmp_path, pkts) -> str:
    path = tmp_path / "test.f1rec"
    with open(path, "wb") as fh:
        for i, p in enumerate(pkts):
            write_record(fh, i * 100, p)
    return str(path)


def test_read_records_round_trip(tmp_path):
    pkts = _packets()
    path = _record_bytes(tmp_path, pkts)
    records = read_records(path)
    assert len(records) == len(pkts)
    assert [p for _ms, p in records] == pkts


def test_replay_matches_direct_process(tmp_path):
    pkts = _packets()
    path = _record_bytes(tmp_path, pkts)

    # Direct: feed each packet to a receiver.
    direct_state = TelemetryState()
    direct = TelemetryReceiver(direct_state, port=0, interested=PACKETS_CONSUMED)
    for p in pkts:
        direct._handle_raw(p)

    # Replay: feed the recorded file through a fresh receiver, no sleeping.
    replay_state = TelemetryState()
    replay = TelemetryReceiver(replay_state, port=0, interested=PACKETS_CONSUMED)
    n = replay_into(replay._handle_raw, path, clock=lambda _s: None)

    assert n == len(pkts)
    d = direct_state.snapshot()
    r = replay_state.snapshot()
    assert d["latest"].get("lap") == r["latest"].get("lap")
    assert d["latest"].get("status") == r["latest"].get("status")
    assert d["packet_counts"] == r["packet_counts"]
    assert d["trends"]["lap_records"] == r["trends"]["lap_records"]


def test_raw_sink_receives_preparse_bytes(tmp_path):
    seen = []
    st = TelemetryState()
    recv = TelemetryReceiver(st, port=0, interested=PACKETS_CONSUMED,
                             raw_sink=seen.append)
    pkts = _packets()[:3]
    for p in pkts:
        recv._handle_raw(p)
    assert seen == pkts


def test_raw_sink_failure_is_contained():
    def bad_sink(_p):
        raise RuntimeError("disk full")

    st = TelemetryState()
    recv = TelemetryReceiver(st, port=0, interested=PACKETS_CONSUMED,
                             raw_sink=bad_sink)
    # Must not raise, and the packet is still parsed into state.
    recv._handle_raw(_packets()[0])
    assert recv.raw_sink_errors == 1
    assert st.packet_counts
