(iou) conor@conor-thinkpad:~/tinker/iou$ cat uring_client.py 
import ctypes
import socket
import os
import struct
import time

# --- FFI Setup ---
lib = ctypes.CDLL('./liburing_helper.so')
lib.uring_init.argtypes = [ctypes.c_uint]
lib.uring_exit.restype = None
lib.uring_submit_recv.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint, ctypes.c_ulong]
lib.uring_submit_send.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint, ctypes.c_ulong]
lib.uring_submit_connect.argtypes = [
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_ulong,
]
lib.uring_wait_cqe.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong)]
lib.uring_init(8)

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

class IoUringLoop:
    def __init__(self):
        self._waiters = {}  # user_data: Future
        self._user_data_counter = 1
        self._timers = []
    def run_until_complete(self, coro):
        Task(coro)
        try:
            while self._waiters or self._timers:
                # For sleep: check if any timers are ready
                now = time.monotonic()
                i = 0
                while i < len(self._timers):
                    t, fut = self._timers[i]
                    if now >= t:
                        fut.set_result(None)
                        self._timers.pop(i)
                    else:
                        i += 1
                if not self._waiters:
                    time.sleep(0.01)
                    continue
                res = ctypes.c_int()
                ud = ctypes.c_ulong()
                lib.uring_wait_cqe(ctypes.byref(res), ctypes.byref(ud))
                fut = self._waiters.pop(ud.value, None)
                if fut:
                    fut.set_result(res.value)
        finally:
            lib.uring_exit()

    def submit_recv(self, fd, buf):
        fut = Future()
        ud = self._user_data_counter
        self._user_data_counter += 1
        self._waiters[ud] = fut
        ret = lib.uring_submit_recv(fd, buf, len(buf), ud)
        if ret < 0:
            fut.set_result(ret)
        return fut
    def submit_send(self, fd, buf):
        fut = Future()
        ud = self._user_data_counter
        self._user_data_counter += 1
        self._waiters[ud] = fut
        ret = lib.uring_submit_send(fd, buf, len(buf), ud)
        if ret < 0:
            fut.set_result(ret)
        return fut
    def submit_connect(self, fd, sockaddr, addrlen):
        fut = Future()
        ud = self._user_data_counter
        self._user_data_counter += 1
        self._waiters[ud] = fut
        ret = lib.uring_submit_connect(fd, sockaddr, addrlen, ud)
        if ret < 0:
            fut.set_result(ret)
        return fut
    def sleep(self, delay):
        fut = Future()
        self._timers.append((time.monotonic() + delay, fut))
        return fut


class SockAddrIn(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_ushort),      # ushort, native endian
        ("sin_port", ctypes.c_ushort),        # ushort, network byte order!
        ("sin_addr", ctypes.c_ubyte * 4),     # in_addr (4 bytes)
        ("sin_zero", ctypes.c_ubyte * 8)      # zero padding
    ]

def make_sockaddr_in(host, port):
    addr = SockAddrIn()
    addr.sin_family = socket.AF_INET
    addr.sin_port = socket.htons(port)  # must be network order!
    addr.sin_addr[:] = bytearray(socket.inet_aton(host))
    addr.sin_zero[:] = b"\x00" * 8
    return addr


# --- Echo Client Logic ---

loop = IoUringLoop()

async def echo_client():
    host, port = "127.0.0.1", 43210
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    fd = sock.fileno()
    addr = make_sockaddr_in(host, port)
    print("Connecting to server...")
    res = await loop.submit_connect(fd, ctypes.byref(addr), ctypes.sizeof(addr))
    if res != 0:
        print(f"connect() failed: {res}")
        sock.close()
        return
    print("Connected.")
    pid = os.getpid()
    i = 0
    try:
        while True:
            msg = f"{pid} i={i}\n".encode()
            await loop.submit_send(fd, msg)
            buf = ctypes.create_string_buffer(4096)
            n = await loop.submit_recv(fd, buf)
            if n <= 0:
                print("server closed connection")
                break
            print(f"server echoed: {buf.raw[:n].decode(errors='replace').strip()}")
            i += 1
            await loop.sleep(1)
    finally:
        sock.close()

if __name__ == "__main__":
    loop.run_until_complete(echo_client())
