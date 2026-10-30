"""Run pyo_uring and asyncio+websockets clients side by side on Deribit's live
feed, then print a comparison. All clients connect to the same IP, measure the
same wall-clock window, and run on separate CPUs where possible.

    python -m bench --duration 300

Set DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET to use the unaggregated `raw`
channels (Deribit only serves those to authenticated connections).
"""
import argparse
import datetime
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from websockets.uri import parse_uri

from bench import report
from bench.common import DEFAULT_INSTRUMENT, DEFAULT_URL, credentials

CLIENTS = {"pyo_uring": "bench.client_uring", "asyncio": "bench.client_asyncio"}


def pick_cpus(n):
    """One CPU per client, leaving the first CPU for this process and the system."""
    if not hasattr(os, "sched_getaffinity"):
        return [None] * n
    cpus = sorted(os.sched_getaffinity(0))
    return cpus[1:n + 1] if len(cpus) > n else [None] * n


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duration", type=float, default=120, help="seconds to measure (default 120)")
    p.add_argument("--warmup", type=float, default=10,
                   help="seconds to connect, subscribe and warm up before measuring (default 10)")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--instrument", default=DEFAULT_INSTRUMENT)
    p.add_argument("--interval", choices=["100ms", "raw", "agg2"],
                   help="default: raw with API credentials, otherwise 100ms")
    p.add_argument("--clients", default="pyo_uring,pyo_uring,asyncio,asyncio",
                   help="comma-separated, from: " + ", ".join(CLIENTS) + ". The default runs two "
                        "of each so the report shows how much two identical clients differ.")
    p.add_argument("--no-pin", action="store_true", help="don't pin each client to its own CPU")
    p.add_argument("--out", help="results directory (default results/<timestamp>)")
    args = p.parse_args()

    names = args.clients.split(",")
    unknown = set(names) - set(CLIENTS)
    if unknown:
        p.error(f"unknown client(s): {', '.join(sorted(unknown))}")
    interval = args.interval or ("raw" if credentials() else "100ms")
    wsuri = parse_uri(args.url)
    ip = socket.getaddrinfo(wsuri.host, wsuri.port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    out = Path(args.out or Path("results") / datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    cpus = [None] * len(names) if args.no_pin else pick_cpus(len(names))
    labels = [f"{n}-{names[:i + 1].count(n)}" if names.count(n) > 1 else n
              for i, n in enumerate(names)]

    start_at = time.time() + args.warmup
    end_at = start_at + args.duration
    done = datetime.datetime.fromtimestamp(end_at).strftime("%H:%M:%S")
    print(f"{args.instrument} @ {interval} via {wsuri.host} ({ip}); "
          f"measuring {args.duration:g}s after {args.warmup:g}s warm-up, done at {done}", flush=True)

    procs = []
    for name, label, cpu in zip(names, labels, cpus):
        cmd = [sys.executable, "-m", CLIENTS[name],
               "--url", args.url, "--ip", ip, "--instrument", args.instrument, "--interval", interval,
               "--start-at", repr(start_at), "--end-at", repr(end_at), "--out", str(out / f"{label}.json")]
        if label != name:
            cmd += ["--label", label]
        if cpu is not None:
            cmd += ["--cpu", str(cpu)]
        procs.append(subprocess.Popen(cmd))
    failed = [label for label, proc in zip(labels, procs) if proc.wait() != 0]
    if failed:
        raise SystemExit(f"client(s) failed: {', '.join(failed)}")

    text = report.render([report.load(out / f"{label}.json") for label in labels])
    (out / "report.txt").write_text(text + "\n")
    print()
    print(text)
    print(f"\nRaw results and this report are in {out}/")


if __name__ == "__main__":
    main()
