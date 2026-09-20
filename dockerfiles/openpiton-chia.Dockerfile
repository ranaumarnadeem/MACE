# CHIA worker image for OpenPiton: CHIA's own base (Ray 2.54.0 + Python
# 3.10.19 + chia already installed, Ubuntu 22.04.5) plus everything OpenPiton's
# Ariane flow needs to configure/build/run. Pinned to CHIA's v1.0.1 release tag
# (the version whose pyproject.toml/ChiaDockerfile match the versions named
# above) rather than :latest, so this file's own stated facts can't silently
# go stale under it -- bump both together when picking up a new CHIA release.
#
# Deliberately does NOT COPY an OpenPiton checkout in — the agent edits RTL
# source, so that state should be synced per-run (CHIA's `file_mounts` in
# cluster.yaml, or a bind mount), not baked into the image. It DOES copy in
# scripts/patch_openpiton.sh, intended to run against whatever checkout
# gets mounted.
#
# Currently unused: this image isn't part of the active provisioning path
# (see docs/TECHNICAL_GUIDE.md, "Docker was built, then dropped" -- the
# project's real GCP worker uses cluster/local.yaml's bare-VM
# `setup_commands` instead, which hand-duplicates patch_openpiton.sh's
# logic inline rather than calling this copied script, since worker setup
# there runs before this repo's own code -- the script included -- is
# synced onto the machine). Kept in case a container is wanted again later;
# nothing else in this repo depends on it existing.
#
# Toolchain: a prebuilt riscv64-elf GCC (github.com/riscv-collab, matching
# this image's Ubuntu version) rather than building riscv-gnu-toolchain from
# source per piton/ariane_build_tools.sh — minutes instead of ~an hour, and it
# already covers what piton/tools/bin/rv64_cc needs
# (-march=rv64imafdc -mabi=lp64d). It is a NEWER toolchain than OpenPiton's
# 2019 code was written for, so it needs scripts/patch_openpiton.sh's fixes
# (binutils 2.38+ split zicsr/zifencei out of base RV64I; GCC 15+ defaults to
# C23, breaking the boot ROM's own K&R-style declarations).
#
# Verilator: apt's packaged build. Confirmed on real hardware to matter less
# than expected -- this adapter detects the installed Verilator's major
# version at build time and adds --no-timing only when talking to a v5, so
# both v4 (this image) and v5 (this project's WSL dev host) build OpenPiton's
# Ariane tile correctly.

FROM ghcr.io/ucb-bar/chia:v1.0.1

ARG RISCV_TOOLCHAIN_URL=https://github.com/riscv-collab/riscv-gnu-toolchain/releases/download/2026.08.27/riscv64-elf-ubuntu-22.04-gcc.tar.xz

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      gawk build-essential bison flex texinfo \
      python3-pexpect libusb-1.0-0-dev default-jdk zlib1g-dev \
      valgrind csh device-tree-compiler \
      libbit-vector-perl libelf-dev \
      verilator dos2unix openssh-client rsync \
      curl ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Prebuilt RISC-V toolchain -> /opt/riscv. Skips ariane_build_tools.sh's own
# gcc build (it early-exits once $RISCV/bin exists), so only fesvr/spike/
# riscv-tests would still need that script -- and this project's Verilator-only
# flow doesn't need any of those (OpenPiton judges pass/fail with its own
# testbench monitors, not FESVR).
RUN mkdir -p /opt/riscv \
    && curl -fL "$RISCV_TOOLCHAIN_URL" | tar -xJ -C /opt/riscv --strip-components=1 \
    && /opt/riscv/bin/riscv64-unknown-elf-gcc --version | head -1

COPY scripts/patch_openpiton.sh /opt/mace/patch_openpiton.sh
RUN chmod +x /opt/mace/patch_openpiton.sh

ENV RISCV=/opt/riscv

USER ray
