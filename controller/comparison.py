"""Application-vs-SDN measurement comparison (pure Python, no Ryu needed).

Kept separate from the controller so it can be unit-tested on its own.

Inputs
  sender    dict from the sender's END message   (application, TX side)
  receiver  dict from the receiver's REPORT      (application, RX side)
  ingress   {"packets", "bytes"} OpenFlow per-flow counters on the switch the
            sender is attached to      (None if the flow was never seen)
  egress    same, on the switch the receiver is attached to

Accounting rules (see docs/ARCHITECTURE.md):
  * The application counts UDP payload bytes only.
  * OpenFlow counts whole Ethernet frames: payload + 42 bytes of headers.
  * FIN packets use the same UDP port, so the switches count them too.  They
    are subtracted using the number of FIN copies each side reports.
  * Every throughput is computed over the same window: the sender's elapsed
    time, so differences come from byte accounting, not from clocks.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app"))
import meter_common as mc  # noqa: E402

SUMMARY_FIELDS = [
    "timestamp", "test_id", "src", "dst", "port", "target_mbps", "pkt_size",
    "window_s",
    "app_tx_pkts", "app_rx_pkts", "app_lost_pkts", "app_loss_pct",
    "app_tx_mbps", "app_rx_mbps",
    "sdn_ingress_pkts", "sdn_egress_pkts", "sdn_lost_pkts", "sdn_loss_pct",
    "sdn_egress_mbps_l2", "sdn_egress_mbps_payload",
    "diff_rx_pkts", "diff_loss_pkts",
    "thr_diff_l2_pct", "thr_diff_payload_pct",
    "app_avg_delay_ms", "app_jitter_ms", "notes",
]


def _mbps(nbytes, window):
    if nbytes is None or not window or window <= 0:
        return None
    return nbytes * 8 / window / 1e6


def _pct(part, whole):
    if part is None or not whole:
        return None
    return 100.0 * part / whole


def _rel_diff(measured, reference):
    if measured is None or not reference:
        return None
    return 100.0 * (measured - reference) / reference


def compare_measurements(test, sender, receiver, ingress, egress):
    """Return one flat dict with application metrics, SDN metrics and the
    differences between them."""
    notes = []
    sender = sender or {}
    receiver = receiver or {}
    window = sender.get("elapsed_s")
    fin_tx = sender.get("fin_tx", 0)
    fin_rx = receiver.get("fin_rx", 0)
    fin_l2 = mc.HDR_LEN + mc.L2_OVERHEAD          # one FIN packet on the wire

    # ---- application side ------------------------------------------------
    tx_pkts = sender.get("tx_pkts")
    rx_pkts = receiver.get("rx_pkts")
    rx_bytes = receiver.get("rx_bytes")
    app_lost = tx_pkts - rx_pkts if tx_pkts is not None and rx_pkts is not None \
        else None
    if not sender:
        notes.append("no END from sender")
    if not receiver:
        notes.append("no REPORT from receiver")

    # ---- SDN side (subtract FIN packets the switches also counted) ---------
    ing_pkts = ing_bytes = eg_pkts = eg_bytes = None
    if ingress:
        ing_pkts = ingress["packets"] - fin_tx
        ing_bytes = ingress["bytes"] - fin_tx * fin_l2
    else:
        notes.append("no flow stats at ingress switch")
    if egress:
        eg_pkts = egress["packets"] - fin_rx
        eg_bytes = egress["bytes"] - fin_rx * fin_l2
    else:
        notes.append("no flow stats at egress switch")
    sdn_lost = ing_pkts - eg_pkts if ing_pkts is not None and eg_pkts is not None \
        else None
    eg_payload = eg_bytes - eg_pkts * mc.L2_OVERHEAD if eg_bytes is not None \
        else None

    app_rx_mbps = _mbps(rx_bytes, window)
    sdn_l2_mbps = _mbps(eg_bytes, window)
    sdn_payload_mbps = _mbps(eg_payload, window)

    return {
        "test_id": test.get("test_id"), "src": test.get("src"),
        "dst": test.get("dst"), "port": test.get("port"),
        "target_mbps": test.get("rate_mbps"), "pkt_size": test.get("pkt_size"),
        "window_s": window,
        "app_tx_pkts": tx_pkts, "app_rx_pkts": rx_pkts,
        "app_lost_pkts": app_lost, "app_loss_pct": _pct(app_lost, tx_pkts),
        "app_tx_mbps": _mbps(sender.get("tx_bytes"), window),
        "app_rx_mbps": app_rx_mbps,
        "sdn_ingress_pkts": ing_pkts, "sdn_egress_pkts": eg_pkts,
        "sdn_lost_pkts": sdn_lost, "sdn_loss_pct": _pct(sdn_lost, ing_pkts),
        "sdn_egress_mbps_l2": sdn_l2_mbps,
        "sdn_egress_mbps_payload": sdn_payload_mbps,
        "diff_rx_pkts": eg_pkts - rx_pkts
        if eg_pkts is not None and rx_pkts is not None else None,
        "diff_loss_pkts": sdn_lost - app_lost
        if sdn_lost is not None and app_lost is not None else None,
        "thr_diff_l2_pct": _rel_diff(sdn_l2_mbps, app_rx_mbps),
        "thr_diff_payload_pct": _rel_diff(sdn_payload_mbps, app_rx_mbps),
        "app_avg_delay_ms": receiver.get("avg_delay_ms"),
        "app_jitter_ms": receiver.get("jitter_ms"),
        "notes": "; ".join(notes),
    }


def _f(value, fmt="%.3f"):
    return "n/a" if value is None else fmt % value


def format_report(m):
    """Human-readable comparison block for the controller log."""
    lines = [
        "=" * 66,
        " TEST %s   %s -> %s:%s   target %s Mbps, %s B packets"
        % (m["test_id"], m["src"], m["dst"], m["port"], m["target_mbps"],
           m["pkt_size"]),
        " window (sender elapsed): %s s" % _f(m["window_s"]),
        "-" * 66,
        " %-28s %14s %14s" % ("", "APPLICATION", "SDN (OpenFlow)"),
        " %-28s %14s %14s" % ("packets sent / at ingress",
                              _f(m["app_tx_pkts"], "%d"),
                              _f(m["sdn_ingress_pkts"], "%d")),
        " %-28s %14s %14s" % ("packets received / at egress",
                              _f(m["app_rx_pkts"], "%d"),
                              _f(m["sdn_egress_pkts"], "%d")),
        " %-28s %14s %14s" % ("packets lost",
                              _f(m["app_lost_pkts"], "%d"),
                              _f(m["sdn_lost_pkts"], "%d")),
        " %-28s %13s%% %13s%%" % ("loss", _f(m["app_loss_pct"], "%.2f"),
                                  _f(m["sdn_loss_pct"], "%.2f")),
        " %-28s %14s %14s" % ("throughput, payload (Mbps)",
                              _f(m["app_rx_mbps"]),
                              _f(m["sdn_egress_mbps_payload"])),
        " %-28s %14s %14s" % ("throughput, L2 frames (Mbps)", "-",
                              _f(m["sdn_egress_mbps_l2"])),
        "-" * 66,
        " L2 vs app throughput diff : %s %%   (header overhead, expected)"
        % _f(m["thr_diff_l2_pct"], "%+.2f"),
        " payload-equivalent diff   : %s %%   (should be ~0)"
        % _f(m["thr_diff_payload_pct"], "%+.2f"),
        " delay avg %s ms, jitter %s ms (application only)"
        % (_f(m["app_avg_delay_ms"], "%.2f"), _f(m["app_jitter_ms"], "%.3f")),
    ]
    if m["notes"]:
        lines.append(" NOTES: " + m["notes"])
    lines.append("=" * 66)
    return "\n".join(lines)
