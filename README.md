This is a pedagogical, barebones event loop and I/O library for Python, demonstrating how to build `async/await`-friendly socket servers using only Linux's io_uring -- with zero dependence on epoll, select, or Python's built-in `asyncio` machinery.

If liburing is not already installed:
```
sudo apt-get update
sudo apt-get install liburing-dev
```

In first window:
```
gcc shared -fPIC uring_helper.c -luring -o liburing_helper.so
python uring_loop.py
```

In n-th other window:
```
nc localhost 43210
hello fron $n$
```
