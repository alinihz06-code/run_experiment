#!/usr/bin/env python3
"""
run_experiment.py
Perbandingan switching konvensional (OVS mode standalone / learning switch)
vs SDN OpenFlow (OVS + controller Ryu simple_switch_13) menggunakan Mininet.

Metrik (format CSV "long"):
  first_rtt_ms         RTT ping pertama (cold start: ARP + packet-in pada SDN)
  rtt_avg_ms           rata-rata RTT ping steady-state
  rtt_mdev_ms          mdev RTT (indikasi jitter)
  ping_loss_pct        persentase loss pada ping
  tcp_throughput_mbps  throughput TCP (iperf3)
  udp_throughput_mbps  throughput UDP pada beban tertentu
  udp_loss_pct         packet drop UDP pada beban tertentu
  udp_jitter_ms        jitter UDP pada beban tertentu

Smoke test (cepat, untuk memastikan semuanya jalan):
  sudo python3 run_experiment.py --reps 1 --duration 3 --loads 10 50 \
       --topos linear --linear-sizes 3 --out smoke.csv

Eksperimen penuh (default: 30 pengulangan per skenario):
  sudo python3 run_experiment.py --out hasil.csv
"""
import argparse
import csv
import json
import re
import subprocess
import sys
import time
from functools import partial
from subprocess import DEVNULL

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSBridge, OVSKernelSwitch, RemoteController
from mininet.topo import Topo


# ---------------------------------------------------------------- Topologi
class LinearT(Topo):
    """n switch berantai, masing-masing dengan 1 host (h1 ... hn)."""

    def build(self, n=3, bw=100, delay="1ms"):
        prev = None
        for i in range(1, n + 1):
            s = self.addSwitch(f"s{i}")
            h = self.addHost(f"h{i}")
            self.addLink(h, s, bw=bw, delay=delay)
            if prev:
                self.addLink(prev, s, bw=bw, delay=delay)
            prev = s


class TreeT(Topo):
    """Pohon: kedalaman `depth`, percabangan `fanout`; host ada di daun."""

    def build(self, depth=2, fanout=2, bw=100, delay="1ms"):
        self.hcount = 0
        self.scount = 0
        self._add(depth, fanout, bw, delay)

    def _add(self, depth, fanout, bw, delay):
        if depth == 0:
            self.hcount += 1
            return self.addHost(f"h{self.hcount}")
        self.scount += 1
        sw = self.addSwitch(f"s{self.scount}")
        for _ in range(fanout):
            child = self._add(depth - 1, fanout, bw, delay)
            self.addLink(sw, child, bw=bw, delay=delay)
        return sw


def make_topo(name, size, args):
    """Kembalikan (topo, jumlah_host)."""
    if name == "linear":
        return LinearT(n=size, bw=args.bw, delay=args.delay), size
    if name == "tree":
        return (
            TreeT(depth=size, fanout=args.fanout, bw=args.bw, delay=args.delay),
            args.fanout ** size,
        )
    raise ValueError(f"topologi tidak dikenal: {name}")


# ---------------------------------------------------------------- Parser
def parse_ping(out):
    res = {}
    m = re.search(r"([\d.]+)% packet loss", out)
    if m:
        res["ping_loss_pct"] = float(m.group(1))
    m = re.search(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", out)
    if m:
        res["rtt_avg_ms"] = float(m.group(2))
        res["rtt_mdev_ms"] = float(m.group(4))
    return res


def first_ping(src, dst_ip):
    # LC_NUMERIC=C memaksa titik (bukan koma) sebagai pemisah desimal,
    # supaya ping tidak menolak interval desimal pada sistem berlocale non-English.
    out = src.cmd(f"LC_NUMERIC=C ping -c 1 -W 5 {dst_ip}")
    m = re.search(r"time=([\d.]+) ms", out)
    return float(m.group(1)) if m else None


def iperf_json(client, server_ip, duration, extra=""):
    out = client.cmd(f"iperf3 -c {server_ip} -t {duration} -J {extra}")
    try:
        return json.loads(out[out.index("{"):])
    except ValueError:
        return None


# ---------------------------------------------------------------- Satu run
def run_one(mode, topo_name, size, rep, args, writer):
    ryu = None
    net = None
    rows = []

    def add(metric, value, offered=""):
        if value is not None:
            rows.append([mode, topo_name, size, rep, metric, offered, value])

    try:
        if mode == "sdn":
            ryu = subprocess.Popen(
                args.ryu_cmd.split(), stdout=DEVNULL, stderr=DEVNULL
            )
            time.sleep(args.ryu_wait)  # beri waktu controller siap

        topo, nhosts = make_topo(topo_name, size, args)
        if mode == "sdn":
            net = Mininet(
                topo=topo,
                switch=partial(OVSKernelSwitch, protocols="OpenFlow13"),
                controller=None,
                link=TCLink,
                autoSetMacs=True,
            )
            net.addController("c0", controller=RemoteController,
                              ip="127.0.0.1", port=6653)
        else:
            net = Mininet(
                topo=topo,
                switch=OVSBridge,  # learning switch standalone, tanpa controller
                controller=None,
                link=TCLink,
                autoSetMacs=True,
            )

        net.start()
        if mode == "sdn":
            net.waitConnected(timeout=20)

        src, dst = net.get("h1"), net.get(f"h{nhosts}")
        dst_ip = dst.IP()

        # 1) Latency: ping pertama (cold) lalu steady-state
        add("first_rtt_ms", first_ping(src, dst_ip))
        p = parse_ping(src.cmd(
            f"LC_NUMERIC=C ping -c {args.ping_count} -i 0.2 -W 2 {dst_ip}"))
        for k, v in p.items():
            add(k, v)

        # 2) Throughput TCP & UDP pada beberapa beban
        dst.cmd("iperf3 -s -D")
        time.sleep(1)

        j = iperf_json(src, dst_ip, args.duration)
        if j:
            add("tcp_throughput_mbps",
                j["end"]["sum_received"]["bits_per_second"] / 1e6)

        for load in args.loads:
            j = iperf_json(src, dst_ip, args.duration, f"-u -b {load}M")
            if j:
                s = j["end"]["sum"]
                add("udp_throughput_mbps", s["bits_per_second"] / 1e6, load)
                add("udp_loss_pct", s["lost_percent"], load)
                add("udp_jitter_ms", s["jitter_ms"], load)

        dst.cmd("pkill iperf3")
    except Exception as e:  # jangan hentikan seluruh eksperimen karena satu run gagal
        print(f"  [PERINGATAN] run gagal: {e}", file=sys.stderr)
    finally:
        if net:
            net.stop()
        if ryu:
            ryu.terminate()
            try:
                ryu.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ryu.kill()

    writer.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------- Main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", nargs="+", default=["conventional", "sdn"],
                    choices=["conventional", "sdn"])
    ap.add_argument("--topos", nargs="+", default=["linear", "tree"],
                    choices=["linear", "tree"])
    ap.add_argument("--linear-sizes", nargs="+", type=int, default=[3, 5],
                    help="jumlah switch pada topologi linear")
    ap.add_argument("--tree-depths", nargs="+", type=int, default=[2],
                    help="kedalaman topologi tree")
    ap.add_argument("--fanout", type=int, default=2)
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--duration", type=int, default=10, help="detik per tes iperf3")
    ap.add_argument("--ping-count", type=int, default=20)
    ap.add_argument("--loads", nargs="+", type=int, default=[10, 25, 50, 75, 100, 120],
                    help="beban UDP (Mbps)")
    ap.add_argument("--bw", type=float, default=100, help="kapasitas link (Mbps)")
    ap.add_argument("--delay", default="1ms", help="delay per link")
    ap.add_argument("--ryu-cmd", default="ryu-manager ryu.app.simple_switch_13")
    ap.add_argument("--ryu-wait", type=float, default=3.0)
    ap.add_argument("--out", default="hasil.csv")
    args = ap.parse_args()

    setLogLevel("warning")
    subprocess.run(["mn", "-c"], stdout=DEVNULL, stderr=DEVNULL)

    configs = []
    if "linear" in args.topos:
        configs += [("linear", n) for n in args.linear_sizes]
    if "tree" in args.topos:
        configs += [("tree", d) for d in args.tree_depths]

    total = len(configs) * len(args.modes) * args.reps
    est = args.duration * (1 + len(args.loads)) + args.ping_count * 0.2 + 12
    print(f"Total run: {total}, perkiraan waktu: {total * est / 60:.0f} menit")

    done = 0
    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["mode", "topo", "size", "rep", "metric",
                         "offered_mbps", "value"])
        for topo_name, size in configs:
            for mode in args.modes:
                for rep in range(1, args.reps + 1):
                    done += 1
                    print(f"[{done}/{total}] {mode} | {topo_name}-{size} | rep {rep}")
                    run_one(mode, topo_name, size, rep, args, writer)
                    f.flush()  # data aman kalau proses terhenti di tengah jalan

    print(f"Selesai. Data tersimpan di {args.out}")


if __name__ == "__main__":
    main()
