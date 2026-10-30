"""Code shared by both benchmark clients.

Everything above the socket layer lives here -- the Deribit session (auth,
subscribe), per-message work, and CPU accounting -- so the two clients differ
only in how bytes get from the kernel to `ws.recv()`.
"""
import argparse
import json
import os
import platform
import resource
import ssl
import sys
import time

import websockets

DEFAULT_URL = "wss://www.deribit.com/ws/api/v2"
DEFAULT_INSTRUMENT = "BTC_USDC-PERPETUAL"
# Large enough for a full order-book snapshot.
MAX_MESSAGE_SIZE = 2**24


def client_arg_parser(description):
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--ip", help="connect to this IPv4 address instead of resolving the URL's host")
    p.add_argument("--instrument", default=DEFAULT_INSTRUMENT)
    p.add_argument("--interval", default="100ms", choices=["100ms", "raw", "agg2"],
                   help="Deribit notification interval; 'raw' needs API credentials")
    p.add_argument("--start-at", type=float, default=0.0,
                   help="Unix time to start measuring (messages before it are warm-up)")
    p.add_argument("--end-at", type=float, default=0.0,
                   help="Unix time to stop (default: when the server closes the connection)")
    p.add_argument("--out", required=True, help="where to write the JSON results")
    p.add_argument("--no-rows", action="store_true", help="leave per-message rows out of the results")
    p.add_argument("--cafile", help="trust this CA certificate (for the local replay server)")
    p.add_argument("--cpu", type=int, help="pin this process to one CPU (Linux only)")
    p.add_argument("--label", help="name for this client in logs and reports")
    return p


def pin_cpu(cpu):
    if cpu is not None:
        os.sched_setaffinity(0, {cpu})


def ssl_context(cafile):
    return ssl.create_default_context(cafile=cafile)


def credentials():
    cid = os.environ.get("DERIBIT_CLIENT_ID")
    secret = os.environ.get("DERIBIT_CLIENT_SECRET")
    return (cid, secret) if cid and secret else None


def channels(instrument, interval):
    return [f"book.{instrument}.{interval}", f"trades.{instrument}.{interval}"]


async def _request(ws, req_id, method, params):
    """Send a JSON-RPC request and wait for its response."""
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == req_id:
            if "error" in msg:
                raise RuntimeError(f"Deribit {method} failed: {msg['error']}")
            return msg["result"]


async def run_session(ws, rec, args, closed_exceptions):
    """Subscribe, warm up until --start-at, then record until the connection closes.

    `ws` only needs `async send(str)` and `async recv() -> str`; each client
    stops the session by closing the connection (or cancelling) at --end-at.
    """
    creds = credentials()
    if args.interval == "raw" and not creds:
        raise SystemExit("--interval raw needs DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET")
    if creds:
        await _request(ws, 1, "public/auth", {
            "grant_type": "client_credentials", "client_id": creds[0], "client_secret": creds[1],
        })
    method = "private/subscribe" if creds else "public/subscribe"
    subscribed = await _request(ws, 2, method, {"channels": channels(args.instrument, args.interval)})
    rec.log(f"subscribed to {', '.join(subscribed)}")

    recv = ws.recv
    if args.start_at and time.time() > args.start_at:
        rec.log("warning: subscribed after --start-at, so this client's window starts late")
    while time.time() < args.start_at:
        rec.warm_up(await recv())

    rec.start()
    on_message = rec.on_message
    try:
        while True:
            on_message(await recv())
    except closed_exceptions:
        pass


class Recorder:
    """Records when each message reached the app and how much CPU it cost.

    Each client calls `on_message` the moment a complete text message comes
    out of `ws.recv()`. A row is (local receive time ns, kind, id, exchange
    timestamp ms), where kind is "b" for a book update (id = change_id) or
    "t" for a trade (id = trade_id).
    """

    def __init__(self, name):
        self.name = name
        self.rows = []
        self.messages = 0
        self.chars = 0
        # Time spent in on_message itself (JSON parsing and bookkeeping), so
        # the report can separate app work from the I/O stack's cost.
        self.app_ns = 0
        # Optional client-specific counters (e.g. socket reads); reported as
        # the change over the measured window.
        self.counters = lambda: {}
        self._start = None
        self._stop = None

    def log(self, text):
        print(f"[{self.name}] {text}", file=sys.stderr, flush=True)

    def warm_up(self, text):
        json.loads(text)

    def _snapshot(self):
        # process_time is exact; getrusage's user/sys split is tick-sampled.
        return (time.time_ns(), time.process_time_ns(),
                resource.getrusage(resource.RUSAGE_SELF), self.counters())

    def start(self):
        self._start = self._snapshot()

    def stop(self):
        if self._stop is None and self._start is not None:
            self._stop = self._snapshot()

    def on_message(self, text):
        t = time.time_ns()
        t0 = time.perf_counter_ns()
        msg = json.loads(text)
        self.messages += 1
        self.chars += len(text)
        params = msg.get("params")
        if params is not None:
            data = params["data"]
            if params["channel"].startswith("book."):
                self.rows.append((t, "b", data["change_id"], data["timestamp"]))
            else:
                for trade in data:
                    self.rows.append((t, "t", trade["trade_id"], trade["timestamp"]))
        self.app_ns += time.perf_counter_ns() - t0

    def dump(self, path, args):
        if self._start is None:
            raise SystemExit(f"[{self.name}] never started measuring; was the window too short?")
        self.stop()
        (t0, cpu0, ru0, c0), (t1, cpu1, ru1, c1) = self._start, self._stop
        result = {
            "client": self.name,
            "url": args.url,
            "ip": args.ip,
            "instrument": args.instrument,
            "interval": args.interval,
            "cpu": args.cpu,
            "window_s": (t1 - t0) / 1e9,
            "messages": self.messages,
            "chars": self.chars,
            "cpu_s": (cpu1 - cpu0) / 1e9,
            "app_s": self.app_ns / 1e9,
            "cpu_user_s": ru1.ru_utime - ru0.ru_utime,
            "cpu_sys_s": ru1.ru_stime - ru0.ru_stime,
            "voluntary_ctx_switches": ru1.ru_nvcsw - ru0.ru_nvcsw,
            "involuntary_ctx_switches": ru1.ru_nivcsw - ru0.ru_nivcsw,
            "env": {
                "python": sys.version.split()[0],
                "kernel": platform.release(),
                "websockets": websockets.__version__,
                "openssl": ssl.OPENSSL_VERSION,
            },
            "counters": {k: c1[k] - c0[k] for k in c1},
            "rows": [] if args.no_rows else self.rows,
        }
        with open(path, "w") as f:
            json.dump(result, f)
        self.log(f"{self.messages} messages in {result['window_s']:.1f}s -> {path}")
