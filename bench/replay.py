"""Replay a recorded Deribit feed to each client over local TLS.

The live feed is too slow to show per-message overheads: at a few messages a
second, every message lands on a sleeping process. This records the feed
once, then a local server replays it to one client at a time:

  - flat out, to measure throughput and CPU per message while the client is busy
  - at fixed rates, stamping each message with its send time, so the client
    measures delivery latency (server send -> client app) in microseconds

    python -m bench.replay                    # records 60s of the live feed first
    python -m bench.replay --rates 100,10000 --repeat 5
"""
import argparse
import asyncio
import datetime
import json
import os
import re
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from websockets.frames import Opcode
from websockets.http11 import Request
from websockets.server import ServerProtocol

from bench.common import DEFAULT_INSTRUMENT, DEFAULT_URL, MAX_MESSAGE_SIZE, channels
from bench.report import pct

CLIENTS = {
    "asyncio": ["bench.client_asyncio"],
    "pyo_uring": ["bench.client_uring"],
    # Polls for up to 1s before sleeping, i.e. never sleeps at these rates.
    "pyo_uring+spin": ["bench.client_uring", "--busy-poll", "1"],
}


# -- recording ----------------------------------------------------------------

async def record(path, seconds, instrument):
    """Save live book and trade notifications, one JSON message per line."""
    from websockets.asyncio.client import connect

    request = {"jsonrpc": "2.0", "id": 1, "method": "public/subscribe",
               "params": {"channels": channels(instrument, "100ms")}}
    n = 0
    async with connect(DEFAULT_URL, compression=None, proxy=None, max_size=MAX_MESSAGE_SIZE) as ws:
        await ws.send(json.dumps(request))
        deadline = time.monotonic() + seconds
        with open(path, "w") as f:
            while (left := deadline - time.monotonic()) > 0:
                try:
                    text = await asyncio.wait_for(ws.recv(), left)
                except TimeoutError:
                    break
                # Keep updates and trades; skip RPC replies and the initial full snapshot.
                if '"method":"subscription"' in text and '"type":"snapshot"' not in text:
                    f.write(text + "\n")
                    n += 1
    return n


def make_cert(directory):
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True)
    return cert, key


# -- server -------------------------------------------------------------------

def frame_header(n):
    """Header of a final, unmasked text frame, as servers send them."""
    if n < 126:
        return bytes((0x81, n))
    if n < 1 << 16:
        return bytes((0x81, 126)) + n.to_bytes(2, "big")
    return bytes((0x81, 127)) + n.to_bytes(8, "big")


class ReplayServer(threading.Thread):
    """Serves one client at a time: WebSocket handshake, replies to the
    session's JSON-RPC requests, then streams messages as `scenario` says:
    ("flat", passes) or (rate, count)."""

    def __init__(self, messages, certfile, keyfile, cpu=None):
        super().__init__(daemon=True)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(certfile, keyfile)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.cpu = cpu
        self.scenario = None
        # Fixed rates: each message split around its timestamp values, so the
        # send time can be spliced in with one join.
        self.templates = [re.split(r'(?<="timestamp":)\d+', m) for m in messages]
        # Flat out: every message framed once, as one block.
        self.block = b"".join(frame_header(len(b)) + b for b in (m.encode() for m in messages))

    def run(self):
        if self.cpu is not None:
            os.sched_setaffinity(0, {self.cpu})
        while True:
            raw, _ = self.listener.accept()
            try:
                with self.ctx.wrap_socket(raw, server_side=True) as conn:
                    self._serve(conn)
            except (OSError, ssl.SSLError) as e:
                print(f"[replay server] connection ended early: {e}", file=sys.stderr)
            finally:
                raw.close()

    def _serve(self, conn):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        proto = ServerProtocol(max_size=MAX_MESSAGE_SIZE)

        def receive():
            data = conn.recv(65536)
            if not data:
                raise ConnectionError("client closed the connection")
            proto.receive_data(data)
            return proto.events_received()

        def flush():
            conn.sendall(b"".join(proto.data_to_send()))

        request = None
        while request is None:
            request = next((e for e in receive() if isinstance(e, Request)), None)
        proto.send_response(proto.accept(request))
        flush()

        subscribed = False
        while not subscribed:
            for frame in receive():
                if frame.opcode is not Opcode.TEXT:
                    continue
                req = json.loads(frame.data)
                result = {}
                if req["method"].endswith("/subscribe"):
                    result, subscribed = req["params"]["channels"], True
                proto.send_text(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode())
            flush()

        mode, count = self.scenario
        if mode == "flat":
            for _ in range(count):
                conn.sendall(self.block)
        else:
            self._send_paced(conn, rate=mode, count=count)

        # Close the WebSocket and wait (briefly) for the client's reply.
        proto.send_close()
        flush()
        conn.settimeout(2)
        try:
            while not any(f.opcode is Opcode.CLOSE for f in receive()):
                pass
        except (OSError, ssl.SSLError):
            pass

    def _send_paced(self, conn, rate, count):
        templates, n = self.templates, len(self.templates)
        interval = 1.0 / rate
        clock = time.perf_counter
        next_send = clock()
        for i in range(count):
            while clock() < next_send:
                pass  # spin: precise send times, and the server is never asleep
            stamp = f"{time.time_ns() / 1e6:.3f}"
            payload = stamp.join(templates[i % n]).encode()
            conn.sendall(frame_header(len(payload)) + payload)
            next_send += interval


# -- running and reporting ------------------------------------------------------

def summarize(r):
    n = r["messages"]
    s = {
        "messages": n,
        "rate": n / r["window_s"],
        "cpu_pct": r["cpu_s"] / r["window_s"] * 100,
        "cpu_us": r["cpu_s"] / n * 1e6,
        "json_us": r["app_s"] / n * 1e6,
        "sleeps": r["voluntary_ctx_switches"] / n,
    }
    # One latency per message: the trades in a message share its receive time.
    lat, last = [], None
    for t_ns, _kind, _id, ts_ms in r["rows"]:
        if t_ns != last:
            lat.append(t_ns / 1e3 - ts_ms * 1e3)
            last = t_ns
    if lat:
        lat.sort()
        s.update(p50=pct(lat, 50), p99=pct(lat, 99), max=lat[-1])
    return s


def render(header, scenarios, clients, runs):
    """runs: {(scenario label, client): [summary, ...]}"""
    med = lambda xs: statistics.median(xs) if xs else float("nan")
    cols = ["msgs/s", "CPU %", "CPU µs/msg", "JSON µs/msg", "sleeps/msg", "latency p50 / p99 / max (µs)"]
    width = max(len(c) for c in clients) + 4
    lines = header + ["", " " * width + "  ".join(f"{c:>12}" for c in cols[:5]) + "  " + cols[5]]
    for label, _ in scenarios:
        lines.append(label)
        for client in clients:
            rs = runs[(label, client)]
            cpu = [r["cpu_us"] for r in rs]
            spread = (max(cpu) - min(cpu)) / med(cpu) * 100
            cells = [
                f"{med([r['rate'] for r in rs]):,.0f}",
                f"{med([r['cpu_pct'] for r in rs]):.1f}",
                f"{med(cpu):.1f} ±{spread / 2:.0f}%",
                f"{med([r['json_us'] for r in rs]):.1f}",
                f"{med([r['sleeps'] for r in rs]):.2f}",
            ]
            latency = "-"
            if "p50" in rs[0]:
                latency = " / ".join(f"{med([r[k] for r in rs]):,.0f}" for k in ("p50", "p99", "max"))
            lines.append(f"  {client:<{width - 2}}" + "  ".join(f"{c:>12}" for c in cells) + "  " + latency.rjust(17))
    lines += [
        "",
        "Each value is the median over runs; ± is half the spread of CPU µs/msg across runs.",
        "CPU % is of one core. Latency is from the server stamping a message to the client",
        "handing it to the app, including TLS and the server's own send.",
    ]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--feed", help="recorded messages, one per line "
                                  "(default results/feed-<instrument>.jsonl, recorded if missing)")
    p.add_argument("--record", type=float, default=60,
                   help="seconds of live feed to record when the feed file doesn't exist (default 60)")
    p.add_argument("--instrument", default=DEFAULT_INSTRUMENT)
    p.add_argument("--clients", default=",".join(CLIENTS), help="comma-separated, from: " + ", ".join(CLIENTS))
    p.add_argument("--rates", default="100,1000,10000", help="messages per second for the paced runs")
    p.add_argument("--seconds", type=float, default=5, help="length of each paced run (default 5)")
    p.add_argument("--flat", type=int, default=200_000,
                   help="about how many messages to send flat out (default 200,000; 0 to skip)")
    p.add_argument("--repeat", type=int, default=3, help="runs per client and scenario (default 3)")
    p.add_argument("--out", help="results directory (default results/replay-<timestamp>)")
    args = p.parse_args()

    clients = args.clients.split(",")
    unknown = set(clients) - set(CLIENTS)
    if unknown:
        p.error(f"unknown client(s): {', '.join(sorted(unknown))}")

    feed = Path(args.feed or f"results/feed-{args.instrument}.jsonl")
    if not feed.exists():
        feed.parent.mkdir(parents=True, exist_ok=True)
        print(f"Recording {args.record:g}s of live {args.instrument} data to {feed} ...", flush=True)
        asyncio.run(record(feed, args.record, args.instrument))
    messages = feed.read_text().splitlines()
    if not messages:
        raise SystemExit(f"{feed} has no messages")

    out = Path(args.out or Path("results") / datetime.datetime.now().strftime("replay-%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    cpus = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else []
    server_cpu, client_cpu = (cpus[1], cpus[2]) if len(cpus) >= 3 else (None, None)

    scenarios = []
    if args.flat:
        passes = max(1, round(args.flat / len(messages)))
        scenarios.append((f"flat out, {passes * len(messages):,} msgs", ("flat", passes)))
    for rate in map(int, args.rates.split(",")):
        scenarios.append((f"{rate:,} msgs/s for {args.seconds:g}s", (rate, int(rate * args.seconds))))

    tmp = Path(tempfile.mkdtemp())
    cert, key = make_cert(tmp)
    server = ReplayServer(messages, cert, key, server_cpu)
    server.start()

    runs = {(label, c): [] for label, _ in scenarios for c in clients}
    total, done, env = len(runs) * args.repeat, 0, None
    for rep in range(args.repeat):
        for label, scenario in scenarios:
            for client in clients:  # interleaved, so drift affects every client alike
                server.scenario = scenario
                result_file = tmp / "result.json"
                cmd = [sys.executable, "-m", *CLIENTS[client],
                       "--url", f"wss://localhost:{server.port}/ws/api/v2", "--ip", "127.0.0.1",
                       "--cafile", str(cert), "--out", str(result_file)]
                if scenario[0] == "flat":
                    cmd.append("--no-rows")
                if client_cpu is not None:
                    cmd += ["--cpu", str(client_cpu)]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    raise SystemExit(f"{client} failed on '{label}':\n{proc.stderr}")
                raw = json.loads(result_file.read_text())
                env = env or raw["env"]
                s = summarize(raw)
                runs[(label, client)].append(s)
                done += 1
                lat = f"  p50 {s['p50']:,.0f} µs" if "p50" in s else ""
                print(f"[{done:>3}/{total}] {label:<26} {client:<15} {s['rate']:>9,.0f} msgs/s  "
                      f"CPU {s['cpu_us']:7.1f} µs/msg{lat}", flush=True)

    avg_chars = sum(map(len, messages)) / len(messages)
    header = [
        f"Replay of {len(messages):,} recorded {args.instrument} messages (avg {avg_chars:,.0f} chars) over local TLS",
        f"Python {env['python']}, kernel {env['kernel']}, websockets {env['websockets']}, {args.repeat} runs each",
    ]
    text = render(header, scenarios, clients, runs)
    (out / "report.txt").write_text(text + "\n")
    (out / "runs.json").write_text(json.dumps({f"{k[0]} | {k[1]}": v for k, v in runs.items()}, indent=1))
    print()
    print(text)
    print(f"\nReport and per-run summaries are in {out}/")


if __name__ == "__main__":
    main()
