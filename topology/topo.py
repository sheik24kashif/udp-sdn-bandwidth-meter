#!/usr/bin/env python3
"""Mininet topology for the UDP bandwidth meter (D1).

        h1 (10.0.0.1)                          h2 (10.0.0.2)
              \\                                /
               s1 ======= bottleneck link ======= s2
              /          (--bw/--delay/--loss)    \\
        h3 (10.0.0.3)                          h4 (10.0.0.4)

* All switches are Open vSwitch speaking OpenFlow 1.3 to a remote Ryu
  controller (default 127.0.0.1:6653).
* Host links are 100 Mbit/s.  The s1-s2 link is the configurable bottleneck,
  so loss appears only there and both measurements can be compared.
* The topology is loop-free on purpose (plain L2 learning switch).  An
  alternate path for rerouting is added in D2.

Usage
    sudo python3 topology/topo.py                 # interactive CLI
    sudo python3 topology/topo.py --demo          # automated end-to-end demo
    sudo python3 topology/topo.py --demo --rate 20 --bw 10   # overload demo
"""
import argparse
import os
import sys
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(REPO, "app")


def build(args):
    net = Mininet(controller=None, switch=OVSKernelSwitch, link=TCLink,
                  autoSetMacs=False, build=False)
    net.addController("c0", controller=RemoteController,
                      ip=args.controller_ip, port=args.controller_port)
    s1 = net.addSwitch("s1", protocols="OpenFlow13")
    s2 = net.addSwitch("s2", protocols="OpenFlow13")
    hosts = {}
    for name, ip, sw in (("h1", "10.0.0.1", s1), ("h3", "10.0.0.3", s1),
                         ("h2", "10.0.0.2", s2), ("h4", "10.0.0.4", s2)):
        hosts[name] = net.addHost(
            name, ip=ip + "/24", mac="00:00:00:00:00:0" + name[1])
        net.addLink(hosts[name], sw, bw=100)
    net.addLink(s1, s2, bw=args.bw, delay=args.delay, loss=args.loss,
                max_queue_size=args.queue)
    net.build()
    net.start()
    return net, hosts


def run_demo(net, hosts, args):
    """Start the receiver on h2, warm up the controller, run the sender on h1."""
    h1, h2 = hosts["h1"], hosts["h2"]
    os.makedirs(os.path.join(REPO, "results"), exist_ok=True)

    info("*** Waiting for the controller to configure the switches\n")
    time.sleep(3)
    info("*** pingAll (lets the controller learn every host)\n")
    drop = net.pingAll()
    if drop > 0:
        info("*** WARNING: %.0f%% ping loss - is the Ryu controller running?\n"
             % drop)

    info("*** Starting UDP receiver on h2\n")
    rx_log = open(os.path.join(REPO, "results", "receiver_h2.log"), "w")
    rx = h2.popen(["python3", os.path.join(APP, "udp_receiver.py"),
                   "--max-tests", "1", "--report-ip", h2.IP()],
                  stdout=rx_log, stderr=rx_log)
    time.sleep(1)

    info("*** Running UDP sender on h1: %.1f Mbps, %d B, %.0f s\n"
         % (args.rate, args.size, args.duration))
    out = h1.cmd("python3 %s --dst %s --rate %s --size %d --duration %s"
                 % (os.path.join(APP, "udp_sender.py"), h2.IP(), args.rate,
                    args.size, args.duration))
    print(out)

    info("*** Waiting for the receiver to finish\n")
    try:
        rx.wait(timeout=15)
    except Exception:
        rx.terminate()
    rx_log.close()
    print(open(os.path.join(REPO, "results", "receiver_h2.log")).read())
    info("*** Done. See the Ryu terminal for the comparison table and "
         "results/*.csv for the raw data.\n")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--controller-ip", default="127.0.0.1")
    p.add_argument("--controller-port", type=int, default=6653)
    p.add_argument("--bw", type=float, default=10, help="bottleneck Mbit/s")
    p.add_argument("--delay", default="5ms", help="bottleneck one-way delay")
    p.add_argument("--loss", type=float, default=0, help="bottleneck loss %%")
    p.add_argument("--queue", type=int, default=100,
                   help="bottleneck queue length in packets")
    p.add_argument("--demo", action="store_true",
                   help="run an automated test, then exit")
    p.add_argument("--rate", type=float, default=5, help="demo rate, Mbit/s")
    p.add_argument("--size", type=int, default=1000, help="demo payload bytes")
    p.add_argument("--duration", type=float, default=10, help="demo seconds")
    args = p.parse_args()

    if os.geteuid() != 0:
        sys.exit("Mininet needs root: run with sudo")

    setLogLevel("info")
    net, hosts = build(args)
    try:
        if args.demo:
            run_demo(net, hosts, args)
        else:
            info("*** Hosts: h1 10.0.0.1  h2 10.0.0.2  h3 10.0.0.3  h4 10.0.0.4\n")
            info("*** Try: h2 python3 app/udp_receiver.py &   then   "
                 "h1 python3 app/udp_sender.py --dst 10.0.0.2\n")
            CLI(net)
    finally:
        net.stop()


if __name__ == "__main__":
    main()
