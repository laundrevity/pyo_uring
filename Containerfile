# io_uring is Linux-only, so this image is the easy way to run the demo on
# macOS/Windows (inside `podman machine`) or on Linux distros without apt.
ARG PYTHON_VERSION=3.15

FROM docker.io/library/python:${PYTHON_VERSION}-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev liburing-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY uring_helper.c .
RUN gcc -shared -fPIC -O2 uring_helper.c -luring -o liburing_helper.so

FROM docker.io/library/python:${PYTHON_VERSION}-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends liburing2 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /src/liburing_helper.so .
COPY pyo_uring.py server.py client.py ./
ENV PYTHONUNBUFFERED=1
# Python handles SIGINT, so `podman stop` exits right away instead of waiting 10s.
STOPSIGNAL SIGINT
EXPOSE 43210
CMD ["python", "server.py"]
