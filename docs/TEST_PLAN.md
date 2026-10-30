# D1 Test Plan

Each case lists what to run, what to expect and how to verify. Run the
controller (`ryu-manager controller/traffic_monitor.py`) in one terminal and
Mininet (`sudo python3 topology/topo.py`) in another unless stated otherwise.

## A. Tests that need no Mininet (run anywhere)

| ID | Command | Expected |
|---|---|---|
| A1 | `python3 tests/test_comparison.py` | 3 PASS: lossless flow, lossy flow, missing data |
| A2 | `python3 tests/loopback_test.py` | all 7 checks PASS: START/END/REPORT reach the (mock) controller, tx = rx, rate within 5 % |
| A3 | `python3 tests/test_controller_logic.py` | 5 PASS: host learning, ARP proxy, START installs per-flow rules + ACK, flow-stats CSV, comparison row (needs Ryu) |

## B. End-to-end tests in Mininet

| ID | Scenario | Run | Expected result |
|---|---|---|---|
| B1 | Connectivity | `mininet> pingall` | 0 % dropped |
| B2 | Switches connected, base rules | `mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1` | table-miss rule and the priority-100 control rule (`udp, nw_dst=10.0.0.254, tp_dst=9999`) |
| B3 | Control channel | start receiver, then sender | sender prints `controller ACK: flow rule on 2 switch(es)`; Ryu logs START, END, REPORT |
| B4 | Per-flow rule installed by the app event | during a test: `sh ovs-ofctl -O OpenFlow13 dump-flows s1` | a priority-10 rule for `nw_src=10.0.0.1, nw_dst=10.0.0.2, tp_dst=5001` with `n_packets` growing |
| B5 | Normal load, no loss | `sudo python3 topology/topo.py --demo` (5 Mbps, 1000 B, 10 s) | loss 0 % on both sides; payload-equivalent throughput difference ≈ 0 %; L2 difference ≈ +4.2 % |
| B6 | Overload, loss at the bottleneck | `sudo python3 topology/topo.py --demo --rate 20 --bw 10` | receiver throughput ≈ link rate (about 9.5-10 Mbps), loss around 50 %; **app loss ≈ SDN loss** (same number of packets) |
| B7 | Small packets (header overhead) | `--size 64` / `512` / `1400` at 2 Mbps | L2 vs. payload throughput gap ≈ +65.6 % / +8.2 % / +3.0 %; payload-equivalent still ≈ 0 % |
| B8 | Random link loss | `--loss 5` | about 5 % loss; app and SDN loss agree |
| B9 | No controller reachable | `h1 python3 app/udp_sender.py --dst 10.0.0.2 --no-ctl` | sender and receiver work without SDN; no comparison is produced |
| B10 | Multiple senders | two senders (h1 and h3) to h2 at once | receiver prints two independent tests; each has its own flow rule and comparison row |

## C. What to capture for the D1 demo

1. Terminal screenshot of `pingall` (B1) and the Ryu log showing the switches
   connecting.
2. The START → ACK exchange and the installed flow rule (B3, B4).
3. The comparison table printed by Ryu for B5 and B6.
4. `results/comparison.csv` and `results/sdn_flow_stats.csv` from those runs.

## D. Pass criteria for D1

* The application delivers UDP traffic across the Mininet topology (B5).
* The controller installs a flow rule because of an application event (B3, B4).
* Packet and byte counts from the OpenFlow flow statistics and from the
  application are both collected and shown side by side (B5, B6).
* Observed differences are explained (header overhead, loss location) and not
  just reported.
