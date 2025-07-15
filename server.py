from pyo_uring import run, start_server, StreamReader, StreamWriter


async def handle_client(reader: StreamReader, writer: StreamWriter):
    print("client connected")
    while True:
        msg = await reader.readline()
        if not msg:
            print("client disconnected")
            break
        print(f"echoing: {msg.strip()}")
        await writer.writeline(msg)
    writer.close()


async def main():
    server = await start_server(handle_client, "0.0.0.0", 43210)
    print("server running on 0.0.0.0:43210")
    await server.serve_forever()


if __name__ == "__main__":
    run(main())
