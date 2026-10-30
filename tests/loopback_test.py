#!/usr/bin/env python3
"""End-to-end test of the socket application on loopback (no Mininet/Ryu).

Starts a mock controller, the receiver and the sender, then checks that
  * the sender registered (START) and reported (END) with the controller,
  * the receiver reported (REPORT) with the controller,
  * transmitted == received packets (loopback is lossless),
  * measured throughput is close to the requested rate.

Run:  python3 tests/loopback_test.py
"""
import csv
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, HERE)
from mock_controller import MockController  # noqa: E402

RATE, SIZE, DURATION = 5.0, 1000, 3.0


def read_last(path):
    with open(path) as fh:
        return list(csv.DictReader(fh))[-1]


def main():
    out = tempfile.mkdtemp(prefix="udpmeter_")
    env = dict(os.environ, RESULTS_DIR=out)
    mock = MockController("127.0.0.1", 9999)
    mock.start()
    rx = subprocess.Popen(
        [sys.executable, os.path.join(APP, "udp_receiver.py"), "--bind",
         "127.0.0.1", "--port", "5001", "--ctl-ip", "127.0.0.1",
         "--max-tests", "1", "--grace", "0.3"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.5)
    tx = subprocess.run(
        [sys.executable, os.path.join(APP, "udp_sender.py"), "--dst",
         "127.0.0.1", "--port", "5001", "--rate", str(RATE), "--size",
         str(SIZE), "--duration", str(DURATION), "--ctl-ip", "127.0.0.1",
         "--fin-delay", "0.2"],
        env=env, capture_output=True, text=True, timeout=60)
    rx_out, _ = rx.communicate(timeout=30)
    mock.stop()

    print(tx.stdout)
    print(rx_out)

    sender = read_last(os.path.join(out, "app_sender.csv"))
    recv = read_last(os.path.join(out, "app_receiver.csv"))
    cmds = [m["cmd"] for m in mock.messages]
    checks = {
        "controller got START": "START" in cmds,
        "controller got END": "END" in cmds,
        "controller got REPORT": "REPORT" in cmds,
        "tx == rx packets": int(sender["tx_pkts"]) == int(recv["rx_pkts"]),
        "no packet loss": int(recv["lost_pkts"]) == 0,
        "throughput within 5% of target":
            abs(float(recv["throughput_mbps"]) - RATE) <= 0.05 * RATE,
        "receiver saw FIN": int(recv["fin_rx"]) >= 1,
    }
    ok = True
    for name, passed in checks.items():
        print("%-34s %s" % (name, "PASS" if passed else "FAIL"))
        ok &= passed
    print("results in", out)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
