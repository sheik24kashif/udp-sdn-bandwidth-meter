"""Ryu (OpenFlow 1.3) controller for the UDP bandwidth meter.

What it does
  1. L2 learning switch (works for the loop-free D1 topology).
  2. Control channel: listens for START / END / REPORT datagrams that the
     socket application sends to a virtual controller address
     (10.0.0.254:9999).  Every switch forwards exactly those datagrams to the
     controller, which answers with a packet-out ACK.
  3. On START it installs a dedicated per-flow rule for the test's UDP flow on
     every switch BEFORE the first data packet is sent, so the OpenFlow
     counters see every packet of the test.
  4. Polls flow statistics every POLL_INTERVAL seconds and logs them to CSV.
  5. When the sender (END) and receiver (REPORT) have both reported, it
     compares the application's numbers with the OpenFlow counters and writes
     one comparison row (loss, throughput, differences).

Run:  ryu-manager controller/traffic_monitor.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import meter_common as mc  # noqa: E402
from comparison import SUMMARY_FIELDS, compare_measurements, format_report  # noqa: E402

from ryu.base import app_manager  # noqa: E402
from ryu.controller import ofp_event  # noqa: E402
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,  # noqa: E402
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub  # noqa: E402
from ryu.lib.packet import (arp, ethernet, ether_types, ipv4, packet,  # noqa: E402
                            udp)
from ryu.ofproto import inet, ofproto_v1_3  # noqa: E402

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
FLOW_PRIORITY = 10          # per-test UDP flow rules
CONTROL_PRIORITY = 100      # control-channel rule (must beat everything)
L2_PRIORITY = 5             # non-UDP IP traffic
FLOW_IDLE_TIMEOUT = 60
REPORT_WAIT = 10.0          # max seconds to wait for the other side's report

STATS_CSV = os.path.join(mc.RESULTS_DIR, "sdn_flow_stats.csv")
STATS_FIELDS = ["timestamp", "dpid", "src", "dst", "udp_dst", "test_id",
                "packets", "bytes", "interval_pkts", "interval_bytes",
                "throughput_mbps"]
COMPARE_CSV = os.path.join(mc.RESULTS_DIR, "comparison.csv")


class TrafficMonitor(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(TrafficMonitor, self).__init__(*args, **kwargs)
        self.datapaths = {}        # dpid -> datapath
        self.mac_to_port = {}      # dpid -> {mac: port}
        self.ip_to_mac = {}        # ip -> mac
        self.host_attach = {}      # ip -> dpid of the switch it hangs off
        self.tests = {}            # "src#test_id" -> test record
        self.latest = {}           # dpid -> {flow_key: {packets, bytes}}
        self.prev = {}             # (dpid, flow_key) -> (time, packets, bytes)
        self.monitor_thread = hub.spawn(self._monitor_loop)
        self.logger.info("TrafficMonitor started, polling every %.1fs, "
                         "results in %s", POLL_INTERVAL, mc.RESULTS_DIR)

    # ------------------------------------------------------------------
    # switch connection handling
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        to_ctrl = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        # table-miss: unknown traffic goes to the controller
        self._add_flow(dp, 0, parser.OFPMatch(), to_ctrl)
        # control channel: START/END/REPORT datagrams go to the controller
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                ip_proto=inet.IPPROTO_UDP,
                                ipv4_dst=mc.CONTROL_IP, udp_dst=mc.CONTROL_PORT)
        self._add_flow(dp, CONTROL_PRIORITY, match, to_ctrl)
        self.logger.info("switch %s connected, base rules installed", dp.id)

    # ------------------------------------------------------------------
    # flow-table helpers
    # ------------------------------------------------------------------
    def _add_flow(self, dp, priority, match, actions, idle=0):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority,
                                      match=match, instructions=inst,
                                      idle_timeout=idle))

    def _del_flow(self, dp, match):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(parser.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE,
                                      out_port=ofp.OFPP_ANY,
                                      out_group=ofp.OFPG_ANY, match=match))

    @staticmethod
    def _flow_match(parser, src, dst, port):
        return parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                               ip_proto=inet.IPPROTO_UDP,
                               ipv4_src=src, ipv4_dst=dst, udp_dst=port)

    # ------------------------------------------------------------------
    # packet-in: learning switch + control channel
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        ip4 = pkt.get_protocol(ipv4.ipv4)
        arp_p = pkt.get_protocol(arp.arp)
        udp_p = pkt.get_protocol(udp.udp) if ip4 else None

        # learn MAC -> port, IP -> MAC and which switch each host hangs off.
        # The first switch that ever sees a host's packet is its own switch.
        self.mac_to_port.setdefault(dp.id, {})[eth.src] = in_port
        src_ip = ip4.src if ip4 else (arp_p.src_ip if arp_p else None)
        if src_ip and src_ip != "0.0.0.0":
            self.ip_to_mac[src_ip] = eth.src
            self.host_attach.setdefault(src_ip, dp.id)

        # ---- ARP proxy: the controller owns the virtual control address --------
        if arp_p and arp_p.opcode == arp.ARP_REQUEST and \
                arp_p.dst_ip == mc.CONTROL_IP:
            self._send_arp_reply(dp, in_port, eth, arp_p)
            return

        # ---- control channel -------------------------------------------
        if udp_p and ip4.dst == mc.CONTROL_IP and udp_p.dst_port == mc.CONTROL_PORT:
            off = 14 + ip4.header_length * 4 + 8
            payload = msg.data[off:off + udp_p.total_length - 8]
            self._handle_control(dp, in_port, eth, ip4, udp_p, payload)
            return

        # ---- forwarding ----------------------------------------------------
        out_port = self.mac_to_port[dp.id].get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD and ip4:
            if ip4.proto == inet.IPPROTO_UDP and udp_p:
                # reactive per-flow rule (fallback if START did not install it)
                self._add_flow(dp, FLOW_PRIORITY,
                               self._flow_match(parser, ip4.src, ip4.dst,
                                                udp_p.dst_port),
                               actions, idle=FLOW_IDLE_TIMEOUT)
            else:
                self._add_flow(dp, L2_PRIORITY,
                               parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                               ip_proto=ip4.proto,
                                               eth_dst=eth.dst),
                               actions, idle=FLOW_IDLE_TIMEOUT)
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions,
                                        data=data))

    # ------------------------------------------------------------------
    # control channel handling (socket application -> SDN controller)
    # ------------------------------------------------------------------
    def _handle_control(self, dp, in_port, eth, ip4, udp_p, payload):
        try:
            msg = json.loads(payload.decode())
            cmd = msg["cmd"]
        except (ValueError, KeyError, UnicodeDecodeError):
            self.logger.warning("bad control message from %s", ip4.src)
            return

        info = "ok"
        if cmd == "START":
            info = self._on_start(ip4.src, msg)
        elif cmd == "END":
            self._on_end(ip4.src, msg)
        elif cmd == "REPORT":
            self._on_report(msg)
        else:
            info = "unknown command"
        self._send_ack(dp, in_port, eth, ip4, udp_p,
                       {"ack": cmd, "test_id": msg.get("test_id"), "info": info})

    @staticmethod
    def _tkey(src, test_id):
        return "%s#%s" % (src, test_id)

    def _on_start(self, src, msg):
        tk = self._tkey(src, msg["test_id"])
        test = {"test_id": msg["test_id"], "src": src, "dst": msg["dst"],
                "port": msg["port"], "rate_mbps": msg.get("rate_mbps"),
                "pkt_size": msg.get("pkt_size"), "duration": msg.get("duration"),
                "t_start": time.time(), "sender": None, "receiver": None,
                "done": False}
        self.tests[tk] = test
        installed = self._install_test_flow(test)
        self.logger.info("START test %s: %s -> %s:%s rate=%s Mbps size=%s B; "
                         "flow rule installed on %d switch(es)", tk, src,
                         test["dst"], test["port"], test["rate_mbps"],
                         test["pkt_size"], installed)
        return "flow rule on %d switch(es)" % installed

    def _install_test_flow(self, test):
        """Delete any old rule for this flow (resets its counters), then
        install a fresh one on every switch that knows the way to the
        destination.  Returns the number of switches that got the rule."""
        dst_mac = self.ip_to_mac.get(test["dst"])
        installed = 0
        for dpid, dp in self.datapaths.items():
            parser = dp.ofproto_parser
            match = self._flow_match(parser, test["src"], test["dst"],
                                     test["port"])
            self._del_flow(dp, match)
            out_port = self.mac_to_port.get(dpid, {}).get(dst_mac)
            if out_port is None:
                self.logger.warning(
                    "switch %s does not know the way to %s (run pingall "
                    "first); falling back to a reactive rule, the first "
                    "packet will not be counted", dpid, test["dst"])
                continue
            self._add_flow(dp, FLOW_PRIORITY, match,
                           [parser.OFPActionOutput(out_port)],
                           idle=FLOW_IDLE_TIMEOUT)
            installed += 1
        return installed

    def _on_end(self, src, msg):
        tk = self._tkey(src, msg["test_id"])
        test = self.tests.get(tk)
        if test is None:
            self.logger.warning("END for unknown test %s", tk)
            return
        test["sender"] = msg
        self.logger.info("END   test %s: sender sent %s packets", tk,
                         msg.get("tx_pkts"))
        self._maybe_finalize(tk)
        hub.spawn(self._watchdog, tk)

    def _on_report(self, msg):
        tk = self._tkey(msg.get("src"), msg["test_id"])
        test = self.tests.get(tk)
        if test is None:
            self.logger.warning("REPORT for unknown test %s", tk)
            return
        test["receiver"] = msg
        self.logger.info("REPORT test %s: receiver got %s packets", tk,
                         msg.get("rx_pkts"))
        self._maybe_finalize(tk)

    def _send_arp_reply(self, dp, in_port, eth, arp_p):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                           dst=eth.src, src=mc.CONTROL_MAC))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=mc.CONTROL_MAC,
                                 src_ip=mc.CONTROL_IP, dst_mac=arp_p.src_mac,
                                 dst_ip=arp_p.src_ip))
        pkt.serialize()
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=ofp.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(in_port)], data=pkt.data))

    def _send_ack(self, dp, in_port, eth, ip4, udp_p, body):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_IP,
                                           dst=eth.src, src=mc.CONTROL_MAC))
        pkt.add_protocol(ipv4.ipv4(proto=inet.IPPROTO_UDP, src=mc.CONTROL_IP,
                                   dst=ip4.src))
        pkt.add_protocol(udp.udp(src_port=mc.CONTROL_PORT,
                                 dst_port=udp_p.src_port))
        pkt.add_protocol(json.dumps(body).encode())
        pkt.serialize()
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=ofp.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(in_port)], data=pkt.data))

    # ------------------------------------------------------------------
    # flow statistics polling
    # ------------------------------------------------------------------
    def _monitor_loop(self):
        while True:
            for dp in list(self.datapaths.values()):
                self._request_flow_stats(dp)
            hub.sleep(POLL_INTERVAL)

    def _request_flow_stats(self, dp):
        dp.send_msg(dp.ofproto_parser.OFPFlowStatsRequest(dp))

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        active = {(t["src"], t["dst"], t["port"]): t["test_id"]
                  for t in self.tests.values() if not t["done"]}
        seen = {}
        for stat in ev.msg.body:
            if stat.priority != FLOW_PRIORITY:
                continue
            try:
                key = (stat.match["ipv4_src"], stat.match["ipv4_dst"],
                       stat.match["udp_dst"])
            except KeyError:
                continue
            seen[key] = {"packets": stat.packet_count, "bytes": stat.byte_count}
            prev = self.prev.get((dpid, key))
            self.prev[(dpid, key)] = (now, stat.packet_count, stat.byte_count)
            d_pkts = d_bytes = 0
            mbps = 0.0
            if prev and now > prev[0] and stat.packet_count >= prev[1]:
                d_pkts = stat.packet_count - prev[1]
                d_bytes = stat.byte_count - prev[2]
                mbps = d_bytes * 8 / (now - prev[0]) / 1e6
            if d_bytes > 0 or key in active:
                mc.append_csv(STATS_CSV, STATS_FIELDS, {
                    "timestamp": round(now, 3), "dpid": dpid, "src": key[0],
                    "dst": key[1], "udp_dst": key[2],
                    "test_id": active.get(key, ""),
                    "packets": stat.packet_count, "bytes": stat.byte_count,
                    "interval_pkts": d_pkts, "interval_bytes": d_bytes,
                    "throughput_mbps": round(mbps, 4)})
        self.latest[dpid] = seen

    # ------------------------------------------------------------------
    # comparison of application vs SDN measurements
    # ------------------------------------------------------------------
    def _maybe_finalize(self, tk):
        test = self.tests[tk]
        if test["done"] or not (test["sender"] and test["receiver"]):
            return
        test["done"] = True
        hub.spawn(self._finalize, tk)

    def _watchdog(self, tk):
        hub.sleep(REPORT_WAIT)
        test = self.tests.get(tk)
        if test and not test["done"]:
            self.logger.warning("test %s: report incomplete after %.0fs, "
                                "finalising with what we have", tk, REPORT_WAIT)
            test["done"] = True
            self._finalize(tk)

    def _finalize(self, tk):
        test = self.tests[tk]
        # fresh counters: ask every switch once more and give replies time
        for dp in list(self.datapaths.values()):
            self._request_flow_stats(dp)
        hub.sleep(0.5)

        key = (test["src"], test["dst"], test["port"])
        ing_dpid = self.host_attach.get(test["src"])
        eg_dpid = self.host_attach.get(test["dst"])
        ingress = self.latest.get(ing_dpid, {}).get(key)
        egress = self.latest.get(eg_dpid, {}).get(key)

        result = compare_measurements(test, test["sender"], test["receiver"],
                                      ingress, egress)
        result["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        mc.append_csv(COMPARE_CSV, SUMMARY_FIELDS, result)
        self.logger.info("\n%s", format_report(result))
