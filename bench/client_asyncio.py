"""Deribit feed client on asyncio and the websockets library (its standard asyncio client)."""
import asyncio
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.uri import parse_uri

from bench.common import MAX_MESSAGE_SIZE, Recorder, client_arg_parser, pin_cpu, run_session, ssl_context


async def main(args, rec):
    options = {"host": args.ip, "port": parse_uri(args.url).port} if args.ip else {}
    if args.cafile:
        options["ssl"] = ssl_context(args.cafile)
    async with connect(
        args.url,
        # Match the pyo_uring client: no compression, no proxy, no client pings.
        compression=None,
        proxy=None,
        ping_interval=None,
        max_size=MAX_MESSAGE_SIZE,
        close_timeout=2,
        **options,
    ) as ws:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (args.end_at - time.time()) if args.end_at else None
        try:
            async with asyncio.timeout_at(deadline):
                await run_session(ws, rec, args, (ConnectionClosed,))
        except TimeoutError:
            pass
        finally:
            rec.stop()


if __name__ == "__main__":
    args = client_arg_parser(__doc__).parse_args()
    pin_cpu(args.cpu)
    rec = Recorder(args.label or "asyncio+websockets")
    asyncio.run(main(args, rec))
    rec.dump(args.out, args)
