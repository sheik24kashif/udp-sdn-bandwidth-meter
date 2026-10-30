#!/usr/bin/env python3
"""UDP measurement traffic generator (the "sender" side of the bandwidth meter).

Sends sequence-numbered, timestamped UDP packets at a fixed rate and counts
what it transmitted.  Before sending it registers the test with the SDN
controller (START) and afterwards reports its counters (END), so the
controller can pre-install a per-flow rule and later compare the application's
numbers with the OpenFlow flow statistics.

Example (inside Mininet, on h1):
    python3 app/udp_sender.py --dst 10.0.0.2 --rate 5 --size 1000 --duration 10
"""
import argparse
import os
import random
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meter_common as mc  # noqa: E402

BURST_MAX = 64   # never send more than this many packets back-to-back

CSV_FIELDS = ["timestamp", "test_id", "dst", "port", "target_mbps", "pkt_size",
              "duration_s", "elapsed_s", "tx_pkts", "tx_bytes",
              "achieved_mbps", "send_errors", "fin_tx"]


def parse_args():
    p = argparse.ArgumentParser(description="UDP measurement traffic sender")
    p.add_argument("--dst", required=True, help="receiver IP address")
    p.add_argument("--port", type=int, default=mc.DEFAULT_DATA_PORT,
                   help="receiver UDP port (default %(default)s)")
    p.add_argument("--rate", type=float, default=5.0,
                   help="target rate in Mbit/s of UDP payload (default %(default)s)")
    p.add_argument("--size", type=int, default=1000,
                   help="UDP payload size in bytes, %d..%d (default %%(default)s)"
                        % (mc.HDR_LEN, mc.MAX_UDP_PAYLOAD))
    p.add_argument("--duration", type=float, default=10.0,
                   help="seconds to send (default %(default)s)")
    p.add_argument("--test-id", type=int, default=None,
                   help="32-bit test id (default: random)")
    p.add_argument("--ctl-ip", default=mc.CONTROL_IP,
                   help="virtual controller IP (default %(default)s)")
    p.add_argument("--ctl-port", type=int, default=mc.CONTROL_PORT)
    p.add_argument("--no-ctl", action="store_true",
                   help="do not talk to the SDN controller (plain UDP test)")
    p.add_argument("--fin-count", type=int, default=5,
                   help="FIN copies sent at the end (UDP may lose some)")
    p.add_argument("--fin-delay", type=float, default=0.5,
                   help="seconds to wait before FIN so queues can drain")
    p.add_argument("--csv", default=os.path.join(mc.RESULTS_DIR, "app_sender.csv"))
    return p.parse_args()


def main():
    args = parse_args()
    if not mc.HDR_LEN <= args.size <= mc.MAX_UDP_PAYLOAD:
        sys.exit("--size must be between %d and %d bytes"
                 % (mc.HDR_LEN, mc.MAX_UDP_PAYLOAD))
    if args.rate <= 0 or args.duration <= 0:
        sys.exit("--rate and --duration must be positive")

    test_id = args.test_id if args.test_id is not None \
        else random.randint(1, 0xFFFFFFFF)
    interval = args.size * 8 / (args.rate * 1e6)      # seconds between packets
    pad = bytes(args.size - mc.HDR_LEN)

    print("[tx] test_id=%d  %s:%d  rate=%.2f Mbps  size=%d B  duration=%.1f s"
          % (test_id, args.dst, args.port, args.rate, args.size, args.duration))

    # ---- 1. register the test with the SDN controller ------------------
    if not args.no_ctl:
        ack = mc.ctl_request(args.ctl_ip, args.ctl_port, {
            "cmd": "START", "role": "sender", "test_id": test_id,
            "dst": args.dst, "port": args.port, "rate_mbps": args.rate,
            "pkt_size": args.size, "duration": args.duration})
        if ack:
            print("[tx] controller ACK: %s" % ack.get("info", "ok"))
        else:
            print("[tx] WARNING: no ACK from controller, sending anyway")

    # ---- 2. send paced UDP traffic --------------------------------------
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    sock.connect((args.dst, args.port))

    tx_pkts = 0         # packets successfully handed to the kernel
    errors = 0
    attempts = 0        # pacing counter (includes failed sends)
    t0 = time.perf_counter()
    t_end = t0 + args.duration
    next_print = t0 + 1.0

    while True:
        now = time.perf_counter()
        if now >= t_end:
            break
        due = int((now - t0) / interval) + 1          # packets owed by now
        for _ in range(min(due - attempts, BURST_MAX)):
            hdr = mc.pack_header(mc.T_DATA, test_id, tx_pkts, time.time())
            attempts += 1
            try:
                sock.send(hdr + pad)
                tx_pkts += 1
            except OSError:
                errors += 1
        if now >= next_print:
            print("[tx] t=%4.1fs  sent=%d" % (now - t0, tx_pkts))
            next_print += 1.0
        wait = t0 + attempts * interval - time.perf_counter()
        if wait > 0.0003:
            time.sleep(wait - 0.0002)

    elapsed = time.perf_counter() - t0
    tx_bytes = tx_pkts * args.size
    achieved = tx_bytes * 8 / elapsed / 1e6

    # ---- 3. FIN: tell the receiver how many packets were sent -----------
    time.sleep(args.fin_delay)
    fin_tx = 0
    fin = mc.pack_header(mc.T_FIN, test_id, tx_pkts, time.time())
    for _ in range(args.fin_count):
        try:
            sock.send(fin)
            fin_tx += 1
        except OSError:
            pass
        time.sleep(0.05)
    sock.close()

    # ---- 4. report our counters to the SDN controller -------------------
    if not args.no_ctl:
        ack = mc.ctl_request(args.ctl_ip, args.ctl_port, {
            "cmd": "END", "role": "sender", "test_id": test_id,
            "tx_pkts": tx_pkts, "tx_bytes": tx_bytes, "elapsed_s": elapsed,
            "achieved_mbps": achieved, "fin_tx": fin_tx,
            "send_errors": errors})
        print("[tx] END reported to controller: %s" % ("ok" if ack else "NO ACK"))

    mc.append_csv(args.csv, CSV_FIELDS, {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "test_id": test_id,
        "dst": args.dst, "port": args.port, "target_mbps": args.rate,
        "pkt_size": args.size, "duration_s": args.duration,
        "elapsed_s": round(elapsed, 4), "tx_pkts": tx_pkts,
        "tx_bytes": tx_bytes, "achieved_mbps": round(achieved, 4),
        "send_errors": errors, "fin_tx": fin_tx})

    print("[tx] DONE  tx_pkts=%d  tx_bytes=%d  achieved=%.3f Mbps (target %.3f)"
          "  send_errors=%d" % (tx_pkts, tx_bytes, achieved, args.rate, errors))
    if achieved < 0.95 * args.rate:
        print("[tx] NOTE: achieved rate is >5%% below target; Python could not "
              "keep up (try a larger --size or a lower --rate)")


if __name__ == "__main__":
    main()
