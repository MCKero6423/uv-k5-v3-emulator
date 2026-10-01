# A box that can run the suite, and build the machine if you give it a QEMU tree.
#
#   docker build -t uvk5 . && docker run --rm uvk5              # unit tests
#   docker build -t uvk5 . && docker run --rm uvk5 bash tools/run_tests.sh
#
# The emulator tests need a patched QEMU build. Fetch a QEMU 7.2 tree first and mount it,
# because tools/setup_qemu.sh patches a tree rather than downloading one:
#
#   docker run --rm -v /path/to/qemu-7.2:/qemu-7.2 -e QEMU_SRC=/qemu-7.2 uvk5 \
#       bash -lc 'bash tools/setup_qemu.sh && bash tools/run_tests.sh'

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash build-essential git ca-certificates curl \
        ninja-build pkg-config python3 \
        libglib2.0-dev libpixman-1-dev libfdt-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . /src

# Fast suite on build: it must pass without a QEMU tree, because that is what a
# contributor has on their first day.
RUN bash tools/run_tests.sh -q

CMD ["bash", "tools/run_tests.sh"]
