#!/bin/bash
# Make an OpenPiton checkout buildable with a modern RISC-V toolchain.
#
# OpenPiton's RV64 boot ROM is 2019 code and its Makefile hardcodes compiler
# flags with plain '=' assignments, so neither the environment nor a sims flag
# can override them -- they have to be patched in the checkout. Run this once
# per checkout (and in the worker image build).
#
# Idempotent: safe to re-run, and safe to run on an already-patched tree.
#
# Usage: scripts/patch_openpiton.sh [PITON_ROOT]
set -euo pipefail

ROOT="${1:-${PITON_ROOT:-}}"
if [ -z "$ROOT" ] || [ ! -d "$ROOT/piton" ]; then
    echo "usage: $0 <piton_root>   (or set PITON_ROOT)" >&2
    exit 2
fi

BOOTROM_MK="$ROOT/piton/design/chipset/rv64_platform/bootrom/linux/Makefile"
[ -f "$BOOTROM_MK" ] || { echo "not found: $BOOTROM_MK" >&2; exit 1; }

changed=0

# 1. binutils 2.38+ split zicsr/zifencei out of base RV64I, so the boot ROM's
#    "csrr s2, mhartid" no longer assembles under -march=rv64imac.
if grep -q -- "-march=rv64imac " "$BOOTROM_MK"; then
    sed -i 's/-march=rv64imac /-march=rv64imac_zicsr_zifencei /' "$BOOTROM_MK"
    echo "patched: zicsr/zifencei added to bootrom -march"
    changed=1
fi

# 2. GCC 15+ defaults to C23, where "void init_uart();" declares a function
#    taking NO arguments rather than an unspecified list -- so the boot ROM's
#    own two-argument call became a hard error. Pin the dialect it was written
#    for instead of editing the sources.
#
#    This one hides: the bootrom's `clean` target removes only the image and
#    the DTB, never the .o files, so a stale main.o masks the failure until
#    something invalidates it (a fresh checkout, a new worker, the image).
if ! grep -q -- "-std=gnu17" "$BOOTROM_MK"; then
    sed -i 's/^\(CFLAGS = .*\)$/\1 -std=gnu17/' "$BOOTROM_MK"
    echo "patched: -std=gnu17 pinned for the bootrom (GCC 15+ defaults to C23)"
    changed=1
fi

if [ "$changed" -eq 0 ]; then
    echo "already patched: $ROOT"
fi

echo "bootrom CFLAGS now:"
grep -n "^CFLAGS" "$BOOTROM_MK"
