"""Summarise benchmark results.

    python -m bench.report results/<run>/pyo_uring.json results/<run>/asyncio.json
"""
import json
import sys


def pct(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    i = round(q / 100 * (len(sorted_vals) - 1))
    return sorted_vals[i]


def _split(user, sys_):
    total = user + sys_
    return f"{user / total * 100:.0f} / {sys_ / total * 100:.0f}" if total else "-"


def summarize(r):
    lat = {"b": [], "t": []}
    for t_ns, kind, _id, exch_ms in r["rows"]:
        lat[kind].append(t_ns / 1e6 - exch_ms)
    for v in lat.values():
        v.sort()
    n = r["messages"]
    cpu = r["cpu_s"]
    s = {
        "window": f"{r['window_s']:.1f}",
        "messages": f"{n:,}",
        "rows": f"{len(lat['b']):,} / {len(lat['t']):,}",
        "rate": f"{n / r['window_s']:.1f}",
        "cpu": f"{cpu * 1e3:.1f}",
        "cpu_split": _split(r["cpu_user_s"], r["cpu_sys_s"]),
        "cpu_per_msg": f"{cpu / n * 1e6:.0f}" if n else "-",
        "app_per_msg": f"{r['app_s'] / n * 1e6:.0f}" if n and "app_s" in r else "-",
        "io_per_msg": f"{(cpu - r['app_s']) / n * 1e6:.0f}" if n and "app_s" in r else "-",
        "cpu_share": f"{cpu / r['window_s'] * 100:.2f}",
        "waits_per_msg": f"{r['voluntary_ctx_switches'] / n:.2f}" if n else "-",
        "preempt": f"{r['involuntary_ctx_switches']:,}",
        "reads_per_msg": "-",
        # Deribit timestamps are whole milliseconds.
        "lat_b": " / ".join(f"{pct(lat['b'], q):.0f}" for q in (50, 90, 99)),
        "lat_t": " / ".join(f"{pct(lat['t'], q):.0f}" for q in (50, 90, 99)),
    }
    reads = r.get("counters", {}).get("recv_calls")
    if reads is not None and n:
        s["reads_per_msg"] = f"{reads / n:.2f}"
    return s


ROWS = [
    ("window", "measured window (s)"),
    ("messages", "messages"),
    ("rows", "  book updates / trades"),
    ("rate", "messages per second"),
    ("cpu", "CPU time (ms)"),
    ("cpu_split", "  user / sys split (%)"),
    ("cpu_per_msg", "CPU per message (µs)"),
    ("app_per_msg", "  app: JSON parse + bookkeeping"),
    ("io_per_msg", "  rest: kernel, loop, TLS, WebSocket"),
    ("cpu_share", "CPU, % of one core"),
    ("waits_per_msg", "sleeps per message [1]"),
    ("preempt", "times preempted"),
    ("reads_per_msg", "socket reads per message"),
    ("lat_b", "book latency p50/90/99 (ms) [2]"),
    ("lat_t", "trade latency p50/90/99 (ms) [2]"),
]


def head_to_head(a, b):
    """Per update seen by both clients: how much later `a` got it than `b` (µs)."""
    seen_b = {(kind, _id): t for t, kind, _id, _ in b["rows"]}
    deltas = []
    for t, kind, _id, _ in a["rows"]:
        tb = seen_b.get((kind, _id))
        if tb is not None:
            deltas.append((t - tb) / 1e3)
    deltas.sort()
    return deltas


def render(results):
    names = [r["client"] for r in results]
    first = results[0]
    env = first["env"]
    lines = [
        f"Deribit {first['instrument']} book + trades @ {first['interval']}  ({first['url']} via {first['ip']})",
        f"Python {env['python']}, kernel {env['kernel']}, websockets {env['websockets']}, {env['openssl']}",
        "",
    ]
    width = max(len(label) for _, label in ROWS) + 2
    summaries = [summarize(r) for r in results]
    col = 3 + max(max(len(n) for n in names), max(len(v) for s in summaries for v in s.values()))
    lines.append(" " * width + "".join(n.rjust(col) for n in names))
    for key, label in ROWS:
        lines.append(label.ljust(width) + "".join(s[key].rjust(col) for s in summaries))

    if len(results) >= 2:
        lines += ["", "Head-to-head [3]: A's arrival time minus B's, on updates both received (µs)"]
        pw = max(len(n) for n in names)
        for i, a in enumerate(results):
            for b in results[i + 1:]:
                d = head_to_head(a, b)
                pair = f"{a['client'].ljust(pw)} vs {b['client'].ljust(pw)}"
                if not d:
                    lines.append(f"  {pair}   no updates in common")
                    continue
                first = sum(x < 0 for x in d) / len(d) * 100
                lines.append(
                    f"  {pair}   n={len(d):<6,} A first {first:3.0f}%   "
                    f"p10 {pct(d, 10):+6.0f}   median {pct(d, 50):+6.0f}   p90 {pct(d, 90):+6.0f}")

    lines += [
        "",
        "[1] voluntary context switches: how often the process blocked waiting for data.",
        "[2] local receive time minus Deribit's timestamp. Includes network time and any",
        "    clock offset between this machine and Deribit, so compare columns, not absolutes.",
        "[3] Each connection has its own delivery delay of a millisecond or more, so these",
        "    show connection luck as much as client speed. Pairs of identical clients show",
        "    the noise floor; a client difference only counts if it stands out from that.",
    ]
    return "\n".join(lines)


def load(path):
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    print(render([load(p) for p in sys.argv[1:]]))
