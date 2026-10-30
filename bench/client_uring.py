"""Deribit feed client on pyo_uring: io_uring sockets, MemoryBIO TLS, sans-I/O websockets."""
import time

from pyo_uring import Task, run, sleep

from bench import uring_ws
from bench.common import MAX_MESSAGE_SIZE, Recorder, client_arg_parser, pin_cpu, run_session, ssl_context


async def stop_at(ws, end_at):
    await sleep(max(0.0, end_at - time.time()))
    ws.abort()


async def main(args, rec):
    ws = await uring_ws.connect(args.url, ip=args.ip, max_size=MAX_MESSAGE_SIZE,
                                ssl_context=ssl_context(args.cafile))
    rec.counters = lambda: {"recv_calls": ws.recv_calls, "bytes_received": ws.bytes_received}
    if args.end_at:
        Task(stop_at(ws, args.end_at))
    try:
        await run_session(ws, rec, args, (uring_ws.ConnectionClosed,))
    finally:
        rec.stop()
        ws.close()


if __name__ == "__main__":
    parser = client_arg_parser(__doc__)
    parser.add_argument("--busy-poll", type=float, default=0.0,
                        help="seconds to poll for completions before sleeping (default 0)")
    args = parser.parse_args()
    pin_cpu(args.cpu)
    rec = Recorder(args.label or ("pyo_uring+spin" if args.busy_poll else "pyo_uring"))
    # Only pass busy_poll when set, so this also runs against older pyo_uring versions.
    run(main(args, rec), **({"busy_poll": args.busy_poll} if args.busy_poll else {}))
    rec.dump(args.out, args)
