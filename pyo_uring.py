import ctypes
import errno
import os
import socket
import threading


lib = ctypes.CDLL(os.path.join(os.path.dirname(os.path.abspath(__file__)), "liburing_helper.so"))
lib.uring_init.argtypes = [ctypes.c_uint]
lib.uring_init.restype = ctypes.c_int
lib.uring_exit.restype = None
lib.uring_prep_recv.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_ulonglong]
lib.uring_prep_send.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_ulonglong]
lib.uring_prep_accept.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong]
lib.uring_prep_connect.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_ulonglong]
lib.uring_prep_timeout.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong]
lib.uring_wait.argtypes = [ctypes.c_longlong, ctypes.POINTER(ctypes.c_int),
                           ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_int]
lib.uring_wait.restype = ctypes.c_int


class KernelTimespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_longlong), ("tv_nsec", ctypes.c_longlong)]


# Helper for sockaddr_in
class SockAddrIn(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_ushort),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_ubyte * 8)
    ]


def make_sockaddr_in(host, port):
    addr = SockAddrIn()
    addr.sin_family = socket.AF_INET
    addr.sin_port = socket.htons(port)
    addr.sin_addr[:] = bytearray(socket.inet_aton(host))
    addr.sin_zero[:] = b"\x00" * 8
    return addr


def _decode(data, encoding):
    try:
        return data.decode(encoding)
    except Exception:
        return data.decode(encoding, errors="replace")


class Future:
    def __init__(self):
        self._done = False
        self._result = None
        self._callbacks = []

    def done(self):
        return self._done

    def set_result(self, result):
        self._done = True
        self._result = result
        for cb in self._callbacks:
            cb(self)

    def result(self):
        return self._result

    def add_done_callback(self, cb):
        if self._done:
            cb(self)
        else:
            self._callbacks.append(cb)

    def __await__(self):
        yield self
        return self.result()


class Task:
    def __init__(self, coro):
        self.coro = coro
        self.step(None)

    def step(self, value):
        try:
            fut = self.coro.send(value)
            fut.add_done_callback(self._wakeup)
        except StopIteration:
            pass

    def _wakeup(self, fut):
        self.step(fut.result())


class Loop:
    _current = None
    _current_lock = threading.Lock()

    def __init__(self, entries=256, busy_poll=0.0):
        ret = lib.uring_init(entries)
        if ret < 0:
            hint = {
                errno.ENOSYS: "Blocked by seccomp (Podman: --security-opt seccomp=unconfined), "
                              "or the kernel lacks io_uring.",
                errno.EACCES: "Blocked by SELinux (Podman: --security-opt label=disable).",
                errno.EPERM: "Disabled by the kernel.io_uring_disabled sysctl, "
                             "or blocked by Docker's seccomp profile.",
            }.get(-ret, "")
            raise OSError(-ret, f"io_uring setup failed: {os.strerror(-ret)}. {hint}")
        self._waiters = {}
        self._user_data_counter = 1
        # Seconds to poll for completions before sleeping in the kernel.
        self.busy_poll = busy_poll
        self._res = (ctypes.c_int * entries)()
        self._ud = (ctypes.c_ulonglong * entries)()

    def run(self, coro):
        with Loop._current_lock:
            Loop._current = self
            try:
                Task(coro)
                # Every pending operation, including sleeps, is an io_uring
                # request, so the loop just waits for completions. Each pass
                # submits everything queued since the last pass and collects
                # every completion that's ready, in at most one system call.
                res, ud, max_n = self._res, self._ud, len(self._res)
                spin_ns = int(self.busy_poll * 1e9)
                wait, handle = lib.uring_wait, self.handle_cqe
                while self._waiters:
                    n = wait(spin_ns, res, ud, max_n)
                    if n == 1:  # the usual case; indexing beats slicing here
                        handle(res[0], ud[0])
                    elif n > 1:
                        for r, u in zip(res[:n], ud[:n]):
                            handle(r, u)
                    elif n < 0:
                        raise OSError(-n, f"io_uring wait failed: {os.strerror(-n)}")
            finally:
                lib.uring_exit()
                Loop._current = None

    @classmethod
    def current(cls):
        return cls._current

    def submit_recv(self, fd, buf):
        fut = Future()
        ud = self._user_data_counter
        self._user_data_counter += 1
        self._waiters[ud] = fut
        ret = lib.uring_prep_recv(fd, buf, len(buf), ud)
        if ret < 0:
            del self._waiters[ud]
            fut.set_result(ret)
        return fut

    def submit_send(self, fd, buf):
        fut = Future()
        ud = self._user_data_counter
        self._user_data_counter += 1
        self._waiters[ud] = fut
        ret = lib.uring_prep_send(fd, buf, len(buf), ud)
        if ret < 0:
            del self._waiters[ud]
            fut.set_result(ret)
        return fut

    def submit_accept(self, fd):
        fut = Future()
        ud = self._user_data_counter
        self._user_data_counter += 1
        addr = ctypes.create_string_buffer(128)
        addrlen = ctypes.c_int(ctypes.sizeof(addr))
        self._waiters[ud] = (fut, addr, addrlen)
        ret = lib.uring_prep_accept(fd, addr, ctypes.byref(addrlen), ud)
        if ret < 0:
            del self._waiters[ud]
            fut.set_result((ret, b""))
        return fut

    def submit_connect(self, fd, sockaddr, addrlen):
        fut = Future()
        ud = self._user_data_counter
        self._user_data_counter += 1
        self._waiters[ud] = fut
        ret = lib.uring_prep_connect(fd, sockaddr, addrlen, ud)
        if ret < 0:
            del self._waiters[ud]
            fut.set_result(ret)
        return fut

    def handle_cqe(self, res, ud):
        item = self._waiters.pop(ud, None)
        if isinstance(item, tuple):  # accept
            fut, addr, addrlen = item
            fut.set_result((res, addr.raw[:addrlen.value]))
        elif item:
            item.set_result(res)

    def sleep(self, delay):
        fut = Future()
        ud = self._user_data_counter
        self._user_data_counter += 1
        self._waiters[ud] = fut
        ns = max(0, int(delay * 1e9))
        # The kernel reads the timespec when the loop submits the request,
        # after this returns, so the future keeps it alive.
        fut.timespec = KernelTimespec(ns // 1_000_000_000, ns % 1_000_000_000)
        ret = lib.uring_prep_timeout(ctypes.byref(fut.timespec), ud)
        if ret < 0:
            del self._waiters[ud]
            fut.set_result(ret)
        return fut


def run(coro, busy_poll=0.0):
    """Run coro to completion.

    busy_poll: seconds to keep checking for completions before sleeping in the
    kernel. Checking is a memory read, not a system call, so polling avoids
    the cost of waking a sleeping process, at the price of a busy CPU.
    """
    loop = Loop(busy_poll=busy_poll)
    loop.run(coro)


async def sleep(delay):
    loop = Loop.current()
    await loop.sleep(delay)


# --------------------
# High-level Streams API
# --------------------
class StreamReader:
    def __init__(self, sock, encoding="utf-8"):
        self.sock = sock
        self.fd = sock.fileno()
        self.bufsize = 4096
        self.encoding = encoding

    async def read(self, n=None):
        n = n or self.bufsize
        buf = ctypes.create_string_buffer(n)
        num = await Loop.current().submit_recv(self.fd, buf)
        if num <= 0:
            return ''  # treat as closed
        return _decode(buf.raw[:num], self.encoding)

    async def readline(self):
        # naive: keep reading until "\n" seen
        parts = []
        while True:
            chunk = await self.read(self.bufsize)
            if not chunk:
                break
            parts.append(chunk)
            if '\n' in chunk:
                break
        combined = ''.join(parts)
        idx = combined.find('\n')
        if idx >= 0:
            return combined[:idx+1]
        return combined


class StreamWriter:
    def __init__(self, sock, encoding="utf-8"):
        self.sock = sock
        self.fd = sock.fileno()
        self.encoding = encoding

    async def write(self, data):
        if isinstance(data, str):
            data = data.encode(self.encoding)
        await Loop.current().submit_send(self.fd, data)

    async def writeline(self, line):
        await self.write(line if line.endswith('\n') else (line + '\n'))

    def close(self):
        self.sock.close()


async def open_connection(host, port, encoding="utf-8"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    fd = sock.fileno()
    addr = make_sockaddr_in(host, port)
    await Loop.current().submit_connect(fd, ctypes.byref(addr), ctypes.sizeof(addr))
    return StreamReader(sock, encoding), StreamWriter(sock, encoding)


class Server:
    def __init__(self, sock, handler, encoding="utf-8"):
        self.sock = sock
        self.fd = sock.fileno()
        self.handler = handler
        self.encoding = encoding

    async def serve_forever(self):
        while True:
            client_fd, _ = await Loop.current().submit_accept(self.fd)
            conn = socket.socket(fileno=client_fd)
            Task(self.handler(
                StreamReader(conn, self.encoding),
                StreamWriter(conn, self.encoding)
            ))


async def start_server(handler, host, port, encoding="utf-8"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen()
    sock.setblocking(False)
    return Server(sock, handler, encoding)
