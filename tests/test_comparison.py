#!/usr/bin/env python3
"""Unit tests for controller/comparison.py (no Mininet or Ryu needed).

Run:  python3 tests/test_comparison.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "controller"))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import meter_common as mc  # noqa: E402
from comparison import compare_measurements, format_report  # noqa: E402

TEST = {"test_id": 1, "src": "10.0.0.1", "dst": "10.0.0.2", "port": 5001,
        "rate_mbps": 5, "pkt_size": 1000}


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_lossless_flow():
    """No loss: SDN payload-equivalent throughput equals the app's."""
    n, size, win = 5000, 1000, 8.0
    sender = {"tx_pkts": n, "tx_bytes": n * size, "elapsed_s": win, "fin_tx": 5}
    receiver = {"rx_pkts": n, "rx_bytes": n * size, "fin_rx": 5,
                "avg_delay_ms": 6.0, "jitter_ms": 0.1}
    wire = size + mc.L2_OVERHEAD
    fin = mc.HDR_LEN + mc.L2_OVERHEAD
    ingress = {"packets": n + 5, "bytes": n * wire + 5 * fin}
    egress = {"packets": n + 5, "bytes": n * wire + 5 * fin}
    m = compare_measurements(TEST, sender, receiver, ingress, egress)
    assert m["app_lost_pkts"] == 0 and m["sdn_lost_pkts"] == 0
    assert m["sdn_ingress_pkts"] == n and m["sdn_egress_pkts"] == n
    assert approx(m["app_rx_mbps"], 5.0)
    assert approx(m["sdn_egress_mbps_payload"], 5.0)
    assert approx(m["thr_diff_payload_pct"], 0.0)
    # L2 counters include 42 header bytes per 1000 byte payload = +4.2 %
    assert approx(m["thr_diff_l2_pct"], 4.2, 1e-6), m["thr_diff_l2_pct"]
    assert m["diff_rx_pkts"] == 0 and m["diff_loss_pkts"] == 0
    assert m["notes"] == ""


def test_lossy_flow_both_sides_agree():
    """Loss on the bottleneck: ingress counts everything, egress only what
    survived; app and SDN must report the same number of lost packets."""
    n, rx, size, win = 10000, 7000, 1000, 8.0
    sender = {"tx_pkts": n, "tx_bytes": n * size, "elapsed_s": win, "fin_tx": 5}
    receiver = {"rx_pkts": rx, "rx_bytes": rx * size, "fin_rx": 4}   # 1 FIN lost
    wire = size + mc.L2_OVERHEAD
    fin = mc.HDR_LEN + mc.L2_OVERHEAD
    ingress = {"packets": n + 5, "bytes": n * wire + 5 * fin}
    egress = {"packets": rx + 4, "bytes": rx * wire + 4 * fin}
    m = compare_measurements(TEST, sender, receiver, ingress, egress)
    assert m["app_lost_pkts"] == 3000 and m["sdn_lost_pkts"] == 3000
    assert approx(m["app_loss_pct"], 30.0) and approx(m["sdn_loss_pct"], 30.0)
    assert m["diff_loss_pkts"] == 0
    assert approx(m["thr_diff_payload_pct"], 0.0)


def test_missing_data_is_reported_not_crashing():
    m = compare_measurements(TEST, None, None, None, None)
    assert m["app_lost_pkts"] is None and m["sdn_lost_pkts"] is None
    assert "no END from sender" in m["notes"]
    assert "no flow stats at ingress switch" in m["notes"]
    assert "n/a" in format_report(m)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("all comparison tests passed")
