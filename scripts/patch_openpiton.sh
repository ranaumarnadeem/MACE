#!/bin/bash
# Make an OpenPiton checkout buildable with a modern RISC-V toolchain, and
# (fix 3) fix broken symlinks, (fix 4) fix CRLF shebangs, on a
# Windows-mounted checkout.
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
if ! grep -q -- "-std=gnu17" <(grep "^CFLAGS" "$BOOTROM_MK"); then
    sed -i 's/^\(CFLAGS = .*\)$/\1 -std=gnu17/' "$BOOTROM_MK"
    echo "patched: -std=gnu17 pinned for the bootrom (GCC 15+ defaults to C23)"
    changed=1
fi

if [ "$changed" -eq 0 ]; then
    echo "already patched: $ROOT"
fi

echo "bootrom CFLAGS now:"
grep -n "^CFLAGS" "$BOOTROM_MK"

# 3. Windows-mounted checkouts (WSL over /mnt/c, or plain Windows git) default
#    core.symlinks=false, so every git-tracked symlink materializes as a
#    plain text file containing its own target path instead of a real
#    symlink -- e.g. bootrom/baremetal/rv64_platform.dts becomes 20 bytes of
#    literal text "../rv64_platform.dts" rather than a link to the real DTS
#    file. Nothing in a 1x1 build ever opens these paths, so this hid
#    completely until the first multi-tile build exercised the baremetal
#    bootrom and dtc choked trying to parse a path string as a device tree.
#    Not needed on a native Linux checkout (a GCP worker's ext4 clone, for
#    instance) -- git there defaults to real symlinks already.
(
    cd "$ROOT"
    if git config core.symlinks | grep -q true; then
        echo "core.symlinks already true: $ROOT"
    else
        broken=""
        while IFS= read -r path; do
            [ -n "$path" ] || continue
            [ -L "$path" ] || broken="$broken $path"
        done < <(git ls-files -s -- . | awk '$1 == "120000" {print $4}')
        if [ -n "$broken" ]; then
            git config core.symlinks true
            # shellcheck disable=SC2086
            git checkout -- $broken
            echo "patched: re-checked-out $(echo "$broken" | wc -w) broken symlink(s) as real symlinks"
        else
            echo "no broken symlinks found: $ROOT"
        fi
    fi
)

# 4. Same Windows-checkout root cause, different symptom: git's own CRLF
#    auto-conversion (or a plain Windows checkout) leaves some .py/.sh
#    scripts with a shebang line ending in \r\n. /usr/bin/env then looks up
#    a program literally named e.g. "python3\r", which doesn't exist --
#    "/usr/bin/env: 'python3\r': No such file or directory". Found via
#    piton/design/chip/tile/ariane/corev_apu/rv_plic/rtl/gen_plic_addrmap.py,
#    only reached once a build actually needs the PLIC (multi-tile), same
#    "nothing 1x1 ever touched this" pattern as fix 3. A checkout-wide sweep
#    (not just that one file) since the same root cause hit 66 files across
#    the tree, mostly in the ariane submodule -- fixing one at a time isn't
#    worth the churn once the pattern is this clear. Only strips \r from
#    lines that actually need it (sed 's/\r$//' is a no-op on a clean LF
#    file), so this is safe to run on an already-fixed tree too.
(
    cd "$ROOT"
    fixed=0
    while IFS= read -r -d '' f; do
        head -c 200 "$f" | grep -qP '^#!.*\r$' || continue
        sed -i 's/\r$//' "$f"
        fixed=$((fixed + 1))
    done < <(find . -type f \( -name "*.py" -o -name "*.sh" \) -print0)
    if [ "$fixed" -gt 0 ]; then
        echo "patched: stripped CRLF shebangs from $fixed script(s)"
    else
        echo "no CRLF shebangs found: $ROOT"
    fi
)
