"""A minimal wss:// client on pyo_uring.

Bytes move through io_uring (connect/recv/send SQEs). TLS runs in memory with
ssl.MemoryBIO, as asyncio's own SSL transport does. WebSocket framing uses the
sans-I/O protocol from `websockets`, the same parser the asyncio client uses,
so the benchmark compares I/O layers rather than two WebSocket implementations.
"""
import collections
import ctypes
import os
import socket
import ssl

from websockets.client import ClientProtocol
from websockets.frames import Opcode
from websockets.http11 import Response
from websockets.uri import parse_uri

from pyo_uring import Loop, make_sockaddr_in

# Same read size asyncio's SSL transport uses.
RECV_SIZE = 256 * 1024

TEXT, BINARY, CONT, CLOSE = Opcode.TEXT, Opcode.BINARY, Opcode.CONT, Opcode.CLOSE


class ConnectionClosed(Exception):
    pass


class UringWebSocket:
    def __init__(self, sock, host, max_size, ssl_context):
        self.sock = sock
        self.fd = sock.fileno()
        self.loop = Loop.current()
        self.incoming = ssl.MemoryBIO()
        self.outgoing = ssl.MemoryBIO()
        self.tls = ssl_context.wrap_bio(self.incoming, self.outgoing, server_hostname=host)
        self.protocol = None
        self.max_size = max_size
        # One receive buffer for the life of the connection. io_uring writes
        # into it through the ctypes view; Python reads it through the
        # memoryview without copying.
        self._buf = bytearray(RECV_SIZE)
        self._cbuf = (ctypes.c_char * RECV_SIZE).from_buffer(self._buf)
        self._view = memoryview(self._buf)
        self._messages = collections.deque()
        self._fragments = None
        self._closed = None
        self.recv_calls = 0
        self.bytes_received = 0

    # -- raw socket I/O ------------------------------------------------------

    async def _recv_raw(self):
        """One io_uring recv into the shared buffer. Returns the byte count (0 = EOF)."""
        n = await self.loop.submit_recv(self.fd, self._cbuf)
        self.recv_calls += 1
        if n > 0:
            self.bytes_received += n
        return n

    async def _send_raw(self, data):
        while data:
            n = await self.loop.submit_send(self.fd, data)
            if n < 0:
                raise OSError(-n, f"send failed: {os.strerror(-n)}")
            data = data[n:]

    async def _flush(self, chunks=None):
        """Push pending WebSocket output through TLS and onto the socket."""
        if chunks is None:
            chunks = self.protocol.data_to_send()
        for chunk in chunks:
            if chunk:  # b"" means "half-close"; we never need to.
                self.tls.write(chunk)
        if self.outgoing.pending:
            await self._send_raw(self.outgoing.read())

    # -- setup -----------------------------------------------------------------

    async def _connect_tcp(self, ip, port):
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        addr = make_sockaddr_in(ip, port)
        ret = await self.loop.submit_connect(self.fd, ctypes.byref(addr), ctypes.sizeof(addr))
        if ret < 0:
            raise OSError(-ret, f"connect to {ip}:{port} failed: {os.strerror(-ret)}")

    async def _tls_handshake(self):
        while True:
            try:
                self.tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                pass
            if self.outgoing.pending:
                await self._send_raw(self.outgoing.read())
            n = await self._recv_raw()
            if n <= 0:
                raise ConnectionError("connection closed during TLS handshake")
            self.incoming.write(self._view[:n])
        if self.outgoing.pending:
            await self._send_raw(self.outgoing.read())

    async def _ws_handshake(self, wsuri):
        self.protocol = ClientProtocol(wsuri, max_size=self.max_size)
        self.protocol.send_request(self.protocol.connect())
        await self._flush()
        while True:
            events = await self._receive()
            if self.protocol.handshake_exc is not None:
                raise self.protocol.handshake_exc
            if events and isinstance(events[0], Response):
                self._handle_frames(events[1:])
                return
            if self._closed:
                raise ConnectionError(f"connection closed during WebSocket handshake: {self._closed}")

    # -- receive path ----------------------------------------------------------

    async def _receive(self):
        """Read once from the socket, decrypt, parse, and return protocol events."""
        n = await self._recv_raw()
        if n <= 0:
            self._closed = "EOF" if n == 0 else os.strerror(-n)
            self.protocol.receive_eof()
            return []
        self.incoming.write(self._view[:n])
        tls_read, receive_data = self.tls.read, self.protocol.receive_data
        while True:
            try:
                plaintext = tls_read(RECV_SIZE)
            except ssl.SSLWantReadError:
                break
            except ssl.SSLZeroReturnError:
                self._closed = "TLS close_notify"
                break
            if not plaintext:
                self._closed = "TLS EOF"
                break
            receive_data(plaintext)
        # Pongs, close replies and TLS session messages, if any.
        out = self.protocol.data_to_send()
        if out or self.outgoing.pending:
            await self._flush(out)
        return self.protocol.events_received()

    def _handle_frames(self, frames):
        for frame in frames:
            op = frame.opcode
            if op is TEXT or op is BINARY:
                if frame.fin:
                    self._messages.append(frame.data.decode() if op is TEXT else frame.data)
                else:
                    self._fragments = (op, [frame.data])
            elif op is CONT:
                op0, parts = self._fragments
                parts.append(frame.data)
                if frame.fin:
                    data = b"".join(parts)
                    self._messages.append(data.decode() if op0 is TEXT else data)
                    self._fragments = None
            elif op is CLOSE:
                self._closed = "WebSocket close frame"
            # Pings are answered by the protocol; pongs need nothing.

    # -- public API --------------------------------------------------------------

    async def recv(self):
        """Return the next message (str for text frames, bytes for binary)."""
        messages = self._messages
        while not messages:
            if self._closed:
                raise ConnectionClosed(self._closed)
            self._handle_frames(await self._receive())
        return messages.popleft()

    async def send(self, text):
        self.protocol.send_text(text.encode())
        await self._flush()

    def abort(self):
        """Stop the connection now: any pending recv completes with EOF."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self):
        self.sock.close()


async def connect(uri, ip=None, max_size=2**20, ssl_context=None):
    wsuri = parse_uri(uri)
    if not wsuri.secure:
        raise ValueError("only wss:// URIs are supported")
    if ip is None:
        ip = socket.getaddrinfo(wsuri.host, wsuri.port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    ws = UringWebSocket(sock, wsuri.host, max_size, ssl_context or ssl.create_default_context())
    try:
        await ws._connect_tcp(ip, wsuri.port)
        await ws._tls_handshake()
        await ws._ws_handshake(wsuri)
    except BaseException:
        sock.close()
        raise
    return ws
