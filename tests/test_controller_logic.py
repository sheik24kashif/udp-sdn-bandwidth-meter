#!/usr/bin/env python3
"""Controller logic test using the real Ryu libraries but fake switches.

No Mininet / Open vSwitch needed.  A fake datapath collects everything the
controller sends (each message is serialised, so malformed OpenFlow messages
fail here).  The test replays what the network would do:

  ARP traffic  -> controller learns hosts and which switch they hang off
  ARP for the virtual control IP -> controller answers (ARP proxy)
  START        -> per-flow rule installed on both switches + ACK packet-out
  flow stats   -> controller records counters / throughput
  END + REPORT -> comparison row written, numbers match the application

Run (needs Ryu):   python3 tests/test_controller_logic.py
"""
import csv
import json
import os
import sys
import tempfile
import types

OUT = tempfile.mkdtemp(prefix="udpmeter_ctl_")
os.environ["RESULTS_DIR"] = OUT                      # before importing the app

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "controller"))
sys.path.insert(0, os.path.join(HERE, "..", "app"))

try:
    # Compatibility shim for newer eventlet releases, which dropped a name Ryu
    # imports.  Harmless (a no-op) with the recommended eventlet==0.30.2.
    import eventlet.wsgi
    if not hasattr(eventlet.wsgi, "ALREADY_HANDLED"):
        eventlet.wsgi.ALREADY_HANDLED = object()
except ImportError:
    pass

try:
    from ryu.lib import hub
    from ryu.lib.packet import arp, ethernet, ether_types, ipv4, packet, udp
    from ryu.ofproto import inet, ofproto_v1_3, ofproto_v1_3_parser
except ImportError:
    print("SKIP: Ryu is not installed (pip install ryu eventlet==0.30.2)")
    sys.exit(0)

import meter_common as mc  # noqa: E402
import traffic_monitor as tm  # noqa: E402

MAC = {"h1": "00:00:00:00:00:01", "h2": "00:00:00:00:00:02"}
IP = {"h1": "10.0.0.1", "h2": "10.0.0.2"}


class FakeDP:
    ofproto = ofproto_v1_3
    ofproto_parser = ofproto_v1_3_parser

    def __init__(self, dpid):
        self.id = dpid
        self.sent = []
        self._xid = 1

    def send_msg(self, msg):
        msg.set_xid(self._xid)
        self._xid += 1
        msg.serialize()                    # fails on malformed messages
        self.sent.append(msg)


def packet_in(dp, in_port, pkt):
    pkt.serialize()
    parser = dp.ofproto_parser
    msg = parser.OFPPacketIn(
        datapath=dp, buffer_id=dp.ofproto.OFP_NO_BUFFER,
        total_len=len(pkt.data), reason=dp.ofproto.OFPR_NO_MATCH, table_id=0,
        cookie=0, match=parser.OFPMatch(in_port=in_port), data=bytes(pkt.data))
    return types.SimpleNamespace(msg=msg)


def arp_packet(opcode, src, dst):
    p = packet.Packet()
    eth_dst = "ff:ff:ff:ff:ff:ff" if opcode == arp.ARP_REQUEST else MAC[dst]
    p.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                     dst=eth_dst, src=MAC[src]))
    p.add_protocol(arp.arp(opcode=opcode, src_mac=MAC[src], src_ip=IP[src],
                           dst_mac=MAC[dst] if opcode == arp.ARP_REPLY
                           else "00:00:00:00:00:00", dst_ip=IP[dst]))
    return p


def control_packet(src, body, sport=40000):
    p = packet.Packet()
    p.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_IP,
                                     dst=mc.CONTROL_MAC, src=MAC[src]))
    p.add_protocol(ipv4.ipv4(proto=inet.IPPROTO_UDP, src=IP[src],
                             dst=mc.CONTROL_IP))
    p.add_protocol(udp.udp(src_port=sport, dst_port=mc.CONTROL_PORT))
    p.add_protocol(json.dumps(body).encode())
    return p


def last_packet_out(dp):
    outs = [m for m in dp.sent if type(m).__name__ == "OFPPacketOut"]
    return outs[-1]


def flow_mods(dp, command=None):
    mods = [m for m in dp.sent if type(m).__name__ == "OFPFlowMod"]
    if command is not None:
        mods = [m for m in mods if m.command == command]
    return mods


def stats_reply(dp, packets, nbytes):
    parser = dp.ofproto_parser
    entry = parser.OFPFlowStats(
        table_id=0, duration_sec=10, duration_nsec=0, priority=tm.FLOW_PRIORITY,
        idle_timeout=60, hard_timeout=0, flags=0, cookie=0,
        packet_count=packets, byte_count=nbytes,
        match=parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_src=IP["h1"],
                              ipv4_dst=IP["h2"], udp_dst=5001),
        instructions=[])
    return types.SimpleNamespace(msg=types.SimpleNamespace(datapath=dp,
                                                           body=[entry]))


def main():
    app = tm.TrafficMonitor()
    s1, s2 = FakeDP(1), FakeDP(2)
    app.datapaths = {1: s1, 2: s2}
    # topology: h1 -- s1(port1) s1(port3) -- s2(port3) s2(port1) -- h2

    # --- 1. learning ------------------------------------------------------
    app._packet_in_handler(packet_in(s1, 1, arp_packet(arp.ARP_REQUEST, "h1", "h2")))
    app._packet_in_handler(packet_in(s2, 3, arp_packet(arp.ARP_REQUEST, "h1", "h2")))
    app._packet_in_handler(packet_in(s2, 1, arp_packet(arp.ARP_REPLY, "h2", "h1")))
    app._packet_in_handler(packet_in(s1, 3, arp_packet(arp.ARP_REPLY, "h2", "h1")))
    assert app.host_attach == {IP["h1"]: 1, IP["h2"]: 2}, app.host_attach
    assert app.mac_to_port[1] == {MAC["h1"]: 1, MAC["h2"]: 3}
    assert app.mac_to_port[2] == {MAC["h1"]: 3, MAC["h2"]: 1}
    print("PASS host learning / host-to-switch attachment")

    # --- 2. ARP proxy for the virtual control address -------------------
    p = arp_packet(arp.ARP_REQUEST, "h1", "h2")
    p.protocols[1].dst_ip = mc.CONTROL_IP
    app._packet_in_handler(packet_in(s1, 1, p))
    reply = packet.Packet(last_packet_out(s1).data).get_protocol(arp.arp)
    assert reply.opcode == arp.ARP_REPLY and reply.src_mac == mc.CONTROL_MAC
    assert reply.src_ip == mc.CONTROL_IP and reply.dst_ip == IP["h1"]
    print("PASS ARP proxy answers for the control IP")

    # --- 3. START: per-flow rule on both switches + ACK -------------------
    start = {"cmd": "START", "role": "sender", "test_id": 7, "dst": IP["h2"],
             "port": 5001, "rate_mbps": 5, "pkt_size": 1000, "duration": 8}
    app._packet_in_handler(packet_in(s1, 1, control_packet("h1", start)))
    for dp, want_port in ((s1, 3), (s2, 1)):
        adds = [m for m in flow_mods(dp, dp.ofproto.OFPFC_ADD)
                if m.priority == tm.FLOW_PRIORITY]
        assert len(adds) == 1, "per-flow rule missing on switch %d" % dp.id
        assert adds[0].match["udp_dst"] == 5001
        assert adds[0].match["ipv4_src"] == IP["h1"]
        out = adds[0].instructions[0].actions[0].port
        assert out == want_port, (dp.id, out)
        assert flow_mods(dp, dp.ofproto.OFPFC_DELETE), "counter reset missing"
    ack_pkt = packet.Packet(last_packet_out(s1).data)
    ack_ip, ack_udp = ack_pkt.get_protocol(ipv4.ipv4), ack_pkt.get_protocol(udp.udp)
    assert ack_ip.src == mc.CONTROL_IP and ack_ip.dst == IP["h1"]
    assert ack_udp.dst_port == 40000 and ack_udp.src_port == mc.CONTROL_PORT
    ack = json.loads(ack_pkt.protocols[-1].decode())
    assert ack["ack"] == "START" and ack["test_id"] == 7, ack
    assert last_packet_out(s1).actions[0].port == 1     # back out the ingress port
    print("PASS START installs per-flow rules on both switches and is ACKed")

    # --- 4. flow stats: 5 Mbps x 8 s of 1000 B packets (+5 FIN copies) ----
    n, size = 5000, 1000
    wire, fin = size + mc.L2_OVERHEAD, mc.HDR_LEN + mc.L2_OVERHEAD
    app._flow_stats_reply_handler(stats_reply(s1, n + 5, n * wire + 5 * fin))
    app._flow_stats_reply_handler(stats_reply(s2, n + 5, n * wire + 5 * fin))
    with open(tm.STATS_CSV) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2 and rows[0]["test_id"] == "7", rows
    print("PASS flow statistics recorded to CSV")

    # --- 5. END + REPORT -> comparison ---------------------------------------
    end = {"cmd": "END", "role": "sender", "test_id": 7, "tx_pkts": n,
           "tx_bytes": n * size, "elapsed_s": 8.0, "achieved_mbps": 5.0,
           "fin_tx": 5, "send_errors": 0}
    report = {"cmd": "REPORT", "role": "receiver", "test_id": 7, "src": IP["h1"],
              "rx_pkts": n, "rx_bytes": n * size, "fin_rx": 5,
              "avg_delay_ms": 6.1, "jitter_ms": 0.2, "throughput_mbps": 5.0}
    app._packet_in_handler(packet_in(s1, 1, control_packet("h1", end)))
    app._packet_in_handler(packet_in(s2, 1, control_packet("h2", report, 40001)))
    hub.sleep(1.5)                                       # let _finalize run
    with open(tm.COMPARE_CSV) as fh:
        row = list(csv.DictReader(fh))[-1]
    assert row["app_tx_pkts"] == str(n) and row["app_rx_pkts"] == str(n)
    assert row["sdn_ingress_pkts"] == str(n) and row["sdn_egress_pkts"] == str(n)
    assert float(row["app_rx_mbps"]) == 5.0
    assert abs(float(row["sdn_egress_mbps_payload"]) - 5.0) < 1e-9
    assert abs(float(row["thr_diff_l2_pct"]) - 4.2) < 1e-6
    assert row["notes"] == "", row["notes"]
    print("PASS comparison row: SDN counters match the application numbers")
    print("results in", OUT)
    print("all controller logic tests passed")


if __name__ == "__main__":
    main()
