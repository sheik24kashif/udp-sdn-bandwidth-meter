#!/usr/bin/env python3
"""Stand-in for the SDN controller's control channel (host-only testing).

Listens for the JSON START / END / REPORT datagrams of the socket application
and answers each with an ACK, exactly like controller/traffic_monitor.py does,
so the sender and receiver can be tested on a laptop without Mininet or Ryu.

    python3 tests/mock_controller.py --bind 127.0.0.1
"""
import argparse
import json
import socket
import sys
import threading


class MockController(threading.Thread):
    def __init__(self, bind="127.0.0.1", port=9999):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind, port))
        self.sock.settimeout(0.5)
        self.messages = []
        self.running = True

    def run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode())
            except ValueError:
                continue
            self.messages.append(msg)
            ack = {"ack": msg.get("cmd"), "test_id": msg.get("test_id"),
                   "info": "mock"}
            self.sock.sendto(json.dumps(ack).encode(), addr)

    def stop(self):
        self.running = False
        self.sock.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9999)
    a = ap.parse_args()
    mock = MockController(a.bind, a.port)
    mock.start()
    print("mock controller listening on %s:%d" % (a.bind, a.port))
    try:
        seen = 0
        while True:
            mock.join(0.5)
            while seen < len(mock.messages):
                print(json.dumps(mock.messages[seen]))
                seen += 1
    except KeyboardInterrupt:
        sys.exit(0)
