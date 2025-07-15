import os

from pyo_uring import run, open_connection, sleep


async def main():
    reader, writer = await open_connection("0.0.0.0", 43210)
    print("connected")
    pid = os.getpid()
    i = 0
    while True:
        await writer.writeline(f"{pid=} {i=}")
        resp = await reader.readline()
        if not resp:
            break
        print(f"rcvd: {resp.strip()}")
        await sleep(2)
        i += 1
    writer.close()


if __name__ == "__main__":
    run(main())
