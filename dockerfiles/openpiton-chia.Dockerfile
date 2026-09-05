# CHIA worker image for OpenPiton: CHIA's own base (Ray 2.54.0 + Python
# 3.10.19 + chia already installed) plus the OpenPiton/Ariane toolchain deps.
#
# Deliberately does NOT COPY an OpenPiton checkout in — the agent edits RTL
# source, so that state should be synced per-run (CHIA's `file_mounts` in
# cluster.yaml, or a bind mount), not baked into the image.
#
# NOTE on Verilator: apt's packaged version is used here (a stable tagged
# release) specifically to avoid a bug we hit with a Verilator 5 "devel"
# nightly build, where verilated.mk's precompiled-header rule is missing a
# `-c` flag and fails with "undefined reference to `main`" during
# `sims -vlt_build`. If that exact symptom reappears with apt's version,
# the fix is two `-c` insertions in
# /usr/share/verilator/include/verilated.mk's `%.fast.gch`/`%.slow.gch`
# rules — not yet confirmed necessary here, so not applied blindly.

FROM ghcr.io/ucb-bar/chia:latest

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      gperf autoconf automake autotools-dev \
      libmpc-dev libmpfr-dev libgmp-dev \
      gawk build-essential bison flex texinfo \
      python3-pexpect libusb-1.0-0-dev default-jdk zlib1g-dev \
      valgrind csh device-tree-compiler \
      libbit-vector-perl libelf-dev \
      verilator dos2unix openssh-client rsync \
    && rm -rf /var/lib/apt/lists/*

USER ray
