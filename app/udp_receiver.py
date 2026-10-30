#!/usr/bin/env python3
"""UDP measurement receiver (the "receiver" side of the bandwidth meter).

One UDP socket serves any number of simultaneous senders.  Each
(sender IP, test id) pair is tracked as an independent flow.  A background
thread prints per-second throughput and finalises a flow when its FIN packet
arrives (after a short grace period for stragglers) or when it has been idle
for too long.  Final per-flow metrics are written to CSV and reported to the
SDN controller (REPORT) for comparison with the OpenFlow flow statistics.

Metrics: received packets/bytes, loss (from sequence numbers), duplicates,
out-of-order packets, throughput, one-way delay, RFC 3550 jitter.

Example (inside Mininet, on h2):
    python3 app/udp_receiver.py --port 5001
"""
import argparse
import os
import signal
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meter_common as mc  # noqa: E402

SUMMARY_FIELDS = ["timestamp", "test_id", "src", "expected_pkts", "rx_pkts",
                  "rx_bytes", "lost_pkts", "loss_pct", "duplicates",
                  "out_of_order", "duration_s", "throughput_mbps",
                  "avg_delay_ms", "max_delay_ms", "jitter_ms", "fin_rx"]
SERIES_FIELDS = ["timestamp", "test_id", "src", "interval_s", "pkts", "bytes",
                 "mbps"]


class Flow:
    """State of one (sender, test) measurement."""

    def __init__(self, src, test_id, now):
        self.src, self.test_id = src, test_id
        self.first_rx = self.last_rx = now
        self.first_size = 0
        self.rx_pkts = self.rx_bytes = 0
        self.dups = self.ooo = 0
        self.max_seq = -1
        self.seen = bytearray(4096)          # 1 byte per sequence number
        self.expected = None                 # total sent, from FIN
        self.fin_rx = 0
        self.fin_time = None
        self.delay_sum = self.delay_max = 0.0
        self.jitter = 0.0
        self.prev_transit = None
        self.int_pkts = self.int_bytes = 0   # counters for the 1 s ticker
        self.done = False

    def on_data(self, seq, send_ts, size, now):
        if seq < len(self.seen) and self.seen[seq]:
            self.dups += 1
            return
        if seq >= len(self.seen):
            self.seen.extend(bytes(max(seq + 1 - len(self.seen),
                                       len(self.seen) // 2 + 1024)))
        self.seen[seq] = 1
        if seq < self.max_seq:
            self.ooo += 1
        else:
            self.max_seq = seq
        if self.rx_pkts == 0:
            self.first_rx, self.first_size = now, size
        self.last_rx = now
        self.rx_pkts += 1
        self.rx_bytes += size
        self.int_pkts += 1
        self.int_bytes += size
        # one-way delay (sender and receiver share one clock inside Mininet)
        transit = now - send_ts
        self.delay_sum += transit
        self.delay_max = max(self.delay_max, transit)
        if self.prev_transit is not None:      # RFC 3550 interarrival jitter
            self.jitter += (abs(transit - self.prev_transit) - self.jitter) / 16
        self.prev_transit = transit

    def metrics(self):
        expected = self.expected if self.expected is not None \
            else self.max_seq + 1
        lost = max(0, expected - self.rx_pkts)
        window = self.last_rx - self.first_rx
        # rate between first and last arrival: the first packet only starts
        # the clock, so its bytes are not counted
        thr = (self.rx_bytes - self.first_size) * 8 / window / 1e6 \
            if window > 0 else 0.0
        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "test_id": self.test_id, "src": self.src,
            "expected_pkts": expected, "rx_pkts": self.rx_pkts,
            "rx_bytes": self.rx_bytes, "lost_pkts": lost,
            "loss_pct": round(100.0 * lost / expected, 3) if expected else 0.0,
            "duplicates": self.dups, "out_of_order": self.ooo,
            "duration_s": round(window, 4),
            "throughput_mbps": round(thr, 4),
            "avg_delay_ms": round(1000 * self.delay_sum / self.rx_pkts, 3)
            if self.rx_pkts else 0.0,
            "max_delay_ms": round(1000 * self.delay_max, 3),
            "jitter_ms": round(1000 * self.jitter, 3),
            "fin_rx": self.fin_rx,
        }


class Receiver:
    def __init__(self, args):
        self.args = args
        self.flows = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.finished = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        self.sock.bind((args.bind, args.port))
        self.sock.settimeout(0.5)

    # ---- main thread: receive and account ---------------------------------
    def recv_loop(self):
        print("[rx] listening on %s:%d" % (self.args.bind, self.args.port))
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            now = time.time()
            hdr = mc.parse_header(data)
            if hdr is None:
                continue                      # not our traffic
            pkt_type, test_id, seq, ts = hdr
            with self.lock:
                flow = self.flows.get((addr[0], test_id))
                if flow is None:
                    flow = self.flows[(addr[0], test_id)] = \
                        Flow(addr[0], test_id, now)
                    print("[rx] new test %d from %s" % (test_id, addr[0]))
                if flow.done:
                    continue
                if pkt_type == mc.T_DATA:
                    flow.on_data(seq, ts, len(data), now)
                else:                          # FIN: seq = total DATA sent
                    flow.fin_rx += 1
                    if flow.fin_time is None:
                        flow.fin_time = now
                        flow.expected = seq

    # ---- background thread: per-second report and finalisation -------------
    def ticker(self):
        last_tick = time.time()
        while not self.stop.is_set():
            time.sleep(0.2)
            now = time.time()
            ready = []
            with self.lock:
                if now - last_tick >= 1.0:
                    dt = now - last_tick
                    last_tick = now
                    for f in self.flows.values():
                        if f.done or f.int_pkts == 0:
                            continue
                        mbps = f.int_bytes * 8 / dt / 1e6
                        print("[rx] test %d  %-12s %8.3f Mbps  %6d pkts/s"
                              % (f.test_id, f.src, mbps, f.int_pkts / dt))
                        mc.append_csv(self.args.series_csv, SERIES_FIELDS, {
                            "timestamp": round(now, 3), "test_id": f.test_id,
                            "src": f.src, "interval_s": round(dt, 3),
                            "pkts": f.int_pkts, "bytes": f.int_bytes,
                            "mbps": round(mbps, 4)})
                        f.int_pkts = f.int_bytes = 0
                for f in self.flows.values():
                    if f.done:
                        continue
                    fin_ready = f.fin_time is not None and \
                        now - f.fin_time >= self.args.grace
                    idle = now - f.last_rx >= self.args.idle_timeout
                    if fin_ready or idle:
                        f.done = True
                        ready.append(f)
            for f in ready:
                self.finalize(f)

    def finalize(self, flow):
        m = flow.metrics()
        print("[rx] ---- test %d from %s finished ----" % (flow.test_id, flow.src))
        print("[rx]   expected=%d received=%d lost=%d (%.2f%%)  dup=%d ooo=%d"
              % (m["expected_pkts"], m["rx_pkts"], m["lost_pkts"], m["loss_pct"],
                 m["duplicates"], m["out_of_order"]))
        print("[rx]   throughput=%.3f Mbps  delay avg=%.2f ms max=%.2f ms  "
              "jitter=%.3f ms" % (m["throughput_mbps"], m["avg_delay_ms"],
                                  m["max_delay_ms"], m["jitter_ms"]))
        mc.append_csv(self.args.csv, SUMMARY_FIELDS, m)
        if not self.args.no_ctl:
            report = dict(m, cmd="REPORT", role="receiver")
            report["dst_ip"] = self.args.report_ip or ""
            ack = mc.ctl_request(self.args.ctl_ip, self.args.ctl_port, report)
            print("[rx]   REPORT sent to controller: %s" % ("ok" if ack else "NO ACK"))
        self.finished += 1
        if self.args.max_tests and self.finished >= self.args.max_tests:
            self.stop.set()

    def finalize_remaining(self):
        with self.lock:
            left = [f for f in self.flows.values() if not f.done and f.rx_pkts]
            for f in left:
                f.done = True
        for f in left:
            self.finalize(f)


def parse_args():
    p = argparse.ArgumentParser(description="UDP measurement receiver")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=mc.DEFAULT_DATA_PORT)
    p.add_argument("--grace", type=float, default=0.5,
                   help="seconds to wait after the first FIN for stragglers")
    p.add_argument("--idle-timeout", type=float, default=3.0,
                   help="finalise a flow after this many silent seconds")
    p.add_argument("--max-tests", type=int, default=0,
                   help="exit after this many finished tests (0 = run forever)")
    p.add_argument("--ctl-ip", default=mc.CONTROL_IP)
    p.add_argument("--ctl-port", type=int, default=mc.CONTROL_PORT)
    p.add_argument("--no-ctl", action="store_true",
                   help="do not report to the SDN controller")
    p.add_argument("--report-ip", default="",
                   help="this host's IP (informational, sent in the REPORT)")
    p.add_argument("--csv", default=os.path.join(mc.RESULTS_DIR, "app_receiver.csv"))
    p.add_argument("--series-csv",
                   default=os.path.join(mc.RESULTS_DIR, "app_rx_timeseries.csv"))
    return p.parse_args()


def main():
    rx = Receiver(parse_args())
    signal.signal(signal.SIGINT, lambda *_: rx.stop.set())
    signal.signal(signal.SIGTERM, lambda *_: rx.stop.set())
    t = threading.Thread(target=rx.ticker, daemon=True)
    t.start()
    rx.recv_loop()
    t.join(timeout=1.0)
    rx.finalize_remaining()


if __name__ == "__main__":
    main()
