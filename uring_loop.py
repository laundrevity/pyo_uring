import ctypes
import socket

# --- FFI Setup ---
lib = ctypes.CDLL('./liburing_helper.so')
lib.uring_init.argtypes = [ctypes.c_uint]
lib.uring_exit.restype = None
lib.uring_submit_recv.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint, ctypes.c_ulong]
lib.uring_submit_send.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint, ctypes.c_ulong]
lib.uring_submit_accept.argtypes = [
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_ulong,
]
lib.uring_wait_cqe.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong)]
lib.uring_init(64)

# --- Coroutine/Event Loop Machinery ---

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
    def run_until_complete(self, coro):
        Task(coro)
        try:
            while self._waiters:
                res = ctypes.c_int()
                ud = ctypes.c_ulong()
                lib.uring_wait_cqe(ctypes.byref(res), ctypes.byref(ud))
                self.handle_cqe(res.value, ud.value)
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
    def submit_accept(self, fd):
        fut = Future()
        ud = self._user_data_counter
        self._user_data_counter += 1
        # For sockaddr:
        addr = ctypes.create_string_buffer(128)  # Large enough for IPv6
        addrlen = ctypes.c_int(ctypes.sizeof(addr))
        self._waiters[ud] = (fut, addr, addrlen)
        ret = lib.uring_submit_accept(fd, addr, ctypes.byref(addrlen), ud)
        if ret < 0:
            fut.set_result(ret)
        return fut

    def handle_cqe(self, res, ud):
        item = self._waiters.pop(ud, None)
        if isinstance(item, tuple):  # accept
            fut, addr, addrlen = item
            fut.set_result((res, addr.raw[:addrlen.value]))
        elif item:
            item.set_result(res)

# --- Example: Fully Async Echo Server ---

loop = IoUringLoop()

async def handle_client(conn):
    fd = conn.fileno()
    while True:
        buf = ctypes.create_string_buffer(4096)  # allocate C buffer
        n = await loop.submit_recv(fd, buf)
        if n <= 0:
            break
        await loop.submit_send(fd, buf.raw[:n])
    conn.close()

async def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 43210))
    srv.listen()
    srv.setblocking(False)
    fd = srv.fileno()
    print("Echo server running on 127.0.0.1:43210")
    while True:
        client_fd, _ = await loop.submit_accept(fd)
        conn = socket.socket(fileno=client_fd)
        Task(handle_client(conn))

if __name__ == "__main__":
    loop.run_until_complete(main())
