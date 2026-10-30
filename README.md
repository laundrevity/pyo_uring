This is a pedagogical, barebones event loop and I/O library for Python, demonstrating how to build `async/await`-friendly socket servers using only Linux's io_uring -- with zero dependence on epoll, select, or Python's built-in `asyncio` machinery.

## Running with Podman (any OS)

io_uring is a Linux kernel feature, so on macOS or Windows this runs inside Podman's Linux VM (`podman machine init && podman machine start` the first time).

```
podman build -t pyo-uring .
```

In first window (starts the server):
```
podman run --rm -it --name pyo-uring --security-opt seccomp=unconfined --security-opt label=disable pyo-uring
```

In n-th other window (starts a client):
```
podman exec -it pyo-uring python client.py
```

Podman blocks io_uring two ways by default, so the server needs both flags:
- `seccomp=unconfined`: Podman's default seccomp profile blocks the io_uring syscalls, as Docker's does (`ENOSYS`).
- `label=disable`: on SELinux hosts, including Podman's macOS/Windows VM, the container's SELinux policy doesn't allow creating an io_uring instance (`EACCES`).

Clients started with `podman exec` share the container's settings.

To talk to the server from your host instead, add `-p 43210:43210` to `podman run` and use `nc localhost 43210`.

## Running natively (Linux)

If liburing is not already installed (Debian/Ubuntu):
```
sudo apt-get update
sudo apt-get install liburing-dev
```

In first window:
```
gcc -shared -fPIC uring_helper.c -luring -o liburing_helper.so
python server.py
```

In n-th other window:
```
python client.py
```

Each client sends `pid=... i=...` every 2 seconds and prints the server's echo.

## How the loop waits

Starting an operation only queues a request in io_uring's submission ring. Each pass of the loop submits everything queued and waits for completions in a single system call (`uring_wait` in [uring_helper.c](uring_helper.c)), then handles every completion that's ready. If completions are already waiting, it makes no system call to collect them.

`run(main(), busy_poll=0.001)` makes the loop poll the completion ring for up to that many seconds before sleeping in the kernel. The ring is memory shared with the kernel, so each check is a plain memory read. epoll has no equivalent: each check is an `epoll_wait` system call. Polling cuts latency, at the cost of keeping a CPU busy.

## Benchmark: Deribit live feeds vs asyncio + websockets

[bench/](bench/) compares pyo_uring with the usual way to do this in Python, the `websockets` library on asyncio. Each client subscribes to Deribit's `book.BTC_USDC-PERPETUAL` and `trades.BTC_USDC-PERPETUAL` channels and parses every message.

The two clients share everything above the socket, so the benchmark measures the I/O layer:
- The pyo_uring client ([bench/uring_ws.py](bench/uring_ws.py)) moves bytes with io_uring, runs TLS in memory with `ssl.MemoryBIO` (as asyncio's own SSL transport does), and parses frames with the sans-I/O protocol from `websockets`, which is the same parser the asyncio client uses.
- Both run the same session and per-message code ([bench/common.py](bench/common.py)).
- Both connect to the same server IP with compression and client pings off, measure the same wall-clock window, and run pinned to separate CPUs.

After `podman build -t pyo-uring .`:
```
mkdir -p results
podman run --rm -it --security-opt seccomp=unconfined --security-opt label=disable \
  -v "$PWD/results:/app/results" pyo-uring python -m bench --duration 600
```

By default it runs two of each client at once, so you can see how much two identical clients differ next to how much the two kinds differ. Use `--clients pyo_uring,asyncio` for one of each. Results and the report are saved under `results/<timestamp>/`. To print the report again, run `python -m bench.report results/<run>/*.json`.

Without credentials it uses Deribit's `100ms` channels, about 4 messages a second for this instrument. Deribit only serves the unaggregated `raw` channels to authenticated connections. To use them, pass an API key and the benchmark switches to `raw`:
```
podman run ... -e DERIBIT_CLIENT_ID -e DERIBIT_CLIENT_SECRET pyo-uring python -m bench
```

Reading the report:
- **CPU per message** is the main result: exact process CPU time over the window divided by messages handled.
- **Sleeps per message** counts how often the process blocked waiting for data. **Socket reads per message** (pyo_uring only) counts io_uring recv completions.
- **Latency** is local receive time minus Deribit's millisecond timestamp. It includes network time and clock offset, so compare columns rather than absolute values.
- **Head-to-head** matches each book `change_id` and `trade_id` across clients. Each connection has its own delivery delay, which varies by a millisecond or more between connections. That is much larger than any difference between event loops, so treat the identical-client pairs as the noise floor.

To run natively on Linux, `pip install websockets` and run `python -m bench` from the repo root.

### Replay benchmark

The live feed is too slow to show per-message costs. At a few messages a second, every message lands on an idle process. [bench/replay.py](bench/replay.py) records the live feed once, then a local TLS server replays it to one client at a time:
- **Flat out**, to measure throughput and CPU per message while the client is busy.
- **At fixed rates** (100, 1,000 and 10,000 messages a second by default). The server stamps each message's `timestamp` field with its send time, so latency is measured in microseconds.

It compares `asyncio`, `pyo_uring`, and `pyo_uring+spin` (pyo_uring with `busy_poll`), interleaving the runs and repeating each three times:
```
podman run --rm -it --security-opt seccomp=unconfined --security-opt label=disable \
  -v "$PWD/results:/app/results" pyo-uring python -m bench.replay
```

The first run records 60 seconds of live data to `results/feed-BTC_USDC-PERPETUAL.jsonl`, and later runs reuse it. `--rates`, `--seconds`, `--flat` and `--repeat` change the scenarios. Run `python -m bench.replay --help` for details. Natively, it also needs the `openssl` command to make the server's test certificate.
