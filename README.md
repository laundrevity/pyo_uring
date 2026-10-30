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
