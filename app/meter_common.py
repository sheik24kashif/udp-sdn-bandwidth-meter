"""Shared constants and helpers for the UDP bandwidth meter.

Used by the sender, the receiver and the Ryu controller so that every
component agrees on the packet format, the control channel and the byte
accounting rules.
"""
import csv
import json
import os
import socket
import struct

# --------------------------------------------------------------------------
# Measurement packet format (all integers network byte order)
#
#   0      2     3          7          11            19
#   +------+-----+----------+----------+-------------+----------------...
#   |magic | type| test_id  |   seq    | send_ts (s) | zero padding
#   +------+-----+----------+----------+-------------+----------------...
#
#   type = 1 (DATA): seq = sequence number of this packet (0, 1, 2, ...)
#   type = 2 (FIN) : seq = TOTAL number of DATA packets the sender sent
# --------------------------------------------------------------------------
MAGIC = 0x554D            # ASCII "UM"
T_DATA = 1
T_FIN = 2
HDR_FMT = "!HBIId"
HDR_LEN = struct.calcsize(HDR_FMT)          # 19 bytes

# Bytes added on the wire around the UDP payload: Ethernet 14 + IPv4 20 + UDP 8.
# OpenFlow byte_count counts the whole L2 frame (without FCS), the
# application only counts the UDP payload.  This is the main reason the two
# throughput numbers differ.
L2_OVERHEAD = 14 + 20 + 8
MAX_UDP_PAYLOAD = 1472    # 1500 MTU - 20 IP - 8 UDP, avoids IP fragmentation

DEFAULT_DATA_PORT = 5001

# --------------------------------------------------------------------------
# In-band control channel between the socket application and the controller.
# Hosts send small JSON datagrams to a *virtual* controller address.  Every
# switch has a flow rule that sends exactly these datagrams to the controller
# (packet-in); the controller answers with a packet-out ACK.
# --------------------------------------------------------------------------
CONTROL_IP = "10.0.0.254"
CONTROL_MAC = "00:00:00:00:00:fe"
CONTROL_PORT = 9999

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RESULTS_DIR = os.environ.get("RESULTS_DIR", os.path.join(REPO_ROOT, "results"))


def pack_header(pkt_type, test_id, seq, ts):
    return struct.pack(HDR_FMT, MAGIC, pkt_type, test_id, seq, ts)


def parse_header(data):
    """Return (type, test_id, seq, ts) or None if `data` is not ours."""
    if len(data) < HDR_LEN:
        return None
    magic, pkt_type, test_id, seq, ts = struct.unpack(HDR_FMT, data[:HDR_LEN])
    if magic != MAGIC or pkt_type not in (T_DATA, T_FIN):
        return None
    return pkt_type, test_id, seq, ts


def append_csv(path, header, row):
    """Append one row (dict) to a CSV file, writing the header if it is new."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


def ctl_request(ctl_ip, ctl_port, msg, retries=3, timeout=1.0):
    """Send a JSON control message and wait for the controller's ACK.

    UDP is unreliable, so the request is retried.  Returns the ACK dict, or
    None if the controller never answered (e.g. no SDN controller running).
    """
    data = json.dumps(msg).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for _ in range(retries):
            try:
                sock.sendto(data, (ctl_ip, ctl_port))
                reply, _addr = sock.recvfrom(4096)
                ack = json.loads(reply.decode())
                if ack.get("ack") == msg.get("cmd") and \
                        ack.get("test_id") == msg.get("test_id"):
                    return ack
            except (socket.timeout, ValueError, OSError):
                continue
    finally:
        sock.close()
    return None
