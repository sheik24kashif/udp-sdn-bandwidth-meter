# UDP Bandwidth Meter & Traffic Analyzer (SDN + Socket Programming)

Jackfruit mini project: **development of a UDP-based traffic measurement
application and comparison of application-level bandwidth measurement with SDN
flow statistics.**

A UDP sender generates measurement traffic, a UDP receiver counts what arrives,
and a Ryu OpenFlow controller independently counts the same traffic from the
switches' flow statistics. The two views (application vs. network) are then
compared for throughput and packet loss.

> Status: **D1 (initial demo)**. D2 adds dynamic SDN behaviour (rerouting / rate
> limiting), robustness scenarios and the full performance evaluation. See
> [Roadmap](#roadmap-d2).

## Architecture

```
 h1 (UDP sender)  10.0.0.1 ─┐                     ┌─ 10.0.0.2  h2 (UDP receiver)
 h3 (spare)       10.0.0.3 ─┤ s1 ═══ bottleneck ═══ s2 ├─ 10.0.0.4  h4 (spare)
                            └─ (--bw / --delay / --loss) ┘
                                 │               │
                                 └── OpenFlow 1.3 ──┴──► Ryu controller (traffic_monitor.py)
                                                          · learning switch
                                                          · control channel 10.0.0.254:9999
                                                          · per-flow rules + flow-stats polling
                                                          · app-vs-SDN comparison
```

How the socket application and the SDN network interact:

1. **START** – the sender tells the controller (UDP datagram to the virtual
   address `10.0.0.254:9999`) which flow it is about to send. The controller
   installs a dedicated per-flow rule on every switch, so the OpenFlow counters
   see every packet of the test, and ACKs.
2. **Data** – the sender transmits paced, sequence-numbered UDP packets; the
   receiver counts packets/bytes, loss, delay and jitter.
3. **END / REPORT** – sender and receiver report their counters to the
   controller, which reads the final flow statistics and prints/stores a
   side-by-side comparison.

Details, message formats and the byte-accounting rules are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Repository layout

```
app/udp_sender.py        UDP traffic generator (rate, size, duration, counters)
app/udp_receiver.py      UDP receiver (loss, throughput, delay, jitter, multi-sender)
app/meter_common.py      packet format, constants, control-channel helper
controller/traffic_monitor.py   Ryu app: forwarding, control channel, flow stats
controller/comparison.py        app-vs-SDN comparison logic (unit tested)
topology/topo.py         Mininet topology + one-command demo
tests/                   unit, loopback and controller-logic tests
docs/                    architecture and test plan
results/                 CSV output (created at run time)
```

## Requirements

| Component | Version / note |
|---|---|
| Ubuntu | 20.04 recommended (Mininet VM image also works) |
| Mininet + Open vSwitch | `sudo apt install mininet openvswitch-switch` |
| Python for Mininet and the apps | system `python3` (3.8+) |
| Ryu | `ryu==4.34` **with `eventlet==0.30.2` on Python 3.8/3.9** |

Ryu 4.34 does not run on Python 3.10+ with a matching eventlet. If your system
Python is newer, create a Python 3.9 virtualenv just for Ryu (it runs as a
separate process, so it does not have to match Mininet's Python):

```bash
python3.9 -m venv ~/ryu-env && source ~/ryu-env/bin/activate
pip install "setuptools<58" wheel
pip install --no-build-isolation ryu==4.34 eventlet==0.30.2
```

## Run the demo

**Terminal 1** – controller:

```bash
cd udp-sdn-bandwidth-meter
source ~/ryu-env/bin/activate        # if you used a virtualenv
ryu-manager controller/traffic_monitor.py
```

**Terminal 2** – topology and automated test (needs root):

```bash
cd udp-sdn-bandwidth-meter
sudo mn -c                                   # clean any old Mininet state
sudo python3 topology/topo.py --demo         # 5 Mbps, 1000 B packets, 10 s
```

Overload demo (20 Mbps offered into the 10 Mbps bottleneck, so packets are lost):

```bash
sudo python3 topology/topo.py --demo --rate 20 --bw 10
```

Manual control through the Mininet CLI:

```bash
sudo python3 topology/topo.py
mininet> pingall
mininet> h2 python3 app/udp_receiver.py --max-tests 1 &
mininet> h1 python3 app/udp_sender.py --dst 10.0.0.2 --rate 5 --size 1000 --duration 10
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

### What you should see

The Ryu terminal prints a comparison table at the end of each test. Illustrative
layout and expected values for a lossless 5 Mbps, 1000 B run (your counts will
differ):

```
 TEST 7   10.0.0.1 -> 10.0.0.2:5001   target 5 Mbps, 1000 B packets
                                    APPLICATION   SDN (OpenFlow)
 packets sent / at ingress                 5000             5000
 packets received / at egress              5000             5000
 packets lost                                 0                0
 throughput, payload (Mbps)               5.000            5.000
 throughput, L2 frames (Mbps)                 -            5.210
 L2 vs app throughput diff : +4.20 %   (header overhead, expected)
```

Raw data is written to `results/`:

| File | Content |
|---|---|
| `app_sender.csv` | one row per test: packets/bytes sent, achieved rate |
| `app_receiver.csv` | one row per test: received, loss, throughput, delay, jitter |
| `app_rx_timeseries.csv` | per-second throughput seen by the receiver |
| `sdn_flow_stats.csv` | per-second OpenFlow counters and throughput per switch |
| `comparison.csv` | one row per test: application vs. SDN metrics and differences |

## Why the two throughput numbers differ

The application counts UDP **payload** bytes. OpenFlow counts whole Ethernet
frames, i.e. payload + 42 bytes (14 Ethernet + 20 IP + 8 UDP) per packet. So the
raw SDN number is higher by `42 / packet_size` (+4.2 % for 1000 B, +65.6 % for
64 B). After subtracting the headers the two agree. Packet **loss** is the same
from both views: the ingress switch counts what entered the network, the egress
switch counts what survived the bottleneck.

## Tests (no Mininet needed)

```bash
python3 tests/test_comparison.py        # comparison maths
python3 tests/loopback_test.py          # sender + receiver + mock controller
python3 tests/test_controller_logic.py  # real Ryu libs, fake switches (needs Ryu)
```

The full end-to-end checks in Mininet are listed in
[`docs/TEST_PLAN.md`](docs/TEST_PLAN.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no ACK from controller` in the sender | Ryu is not running, or run `pingall` first. The test still runs but is not compared. |
| Warning "does not know the way to 10.0.0.2" | Run `pingall` first so the controller learns the hosts. Without it the first packet is not counted. |
| `Exception: Error creating interface pair` / stale state | `sudo mn -c` |
| Ryu: `cannot import name 'ALREADY_HANDLED'` or `is_timeout` error | Wrong Python/eventlet combination, see Requirements. |
| Sender says achieved rate is below target | Python cannot keep up at that packet rate; use a larger `--size` or lower `--rate`. |

## Roadmap (D2)

* Alternate path (`s3`) and dynamic SDN logic: congestion-triggered rerouting
  and/or OpenFlow-meter rate limiting driven by flow statistics.
* Challenge scenarios: link failure, loss bursts, competing flows.
* Experiment scripts and graphs: loads of 1/5/10/20 Mbps, packet sizes
  64/512/1400 B, static-forwarding baseline vs. dynamic SDN.
