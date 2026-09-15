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

# 5. Verilator's --coverage-* flags instrument the model, but the hand-written
#    testbench Verilator links against never calls Verilator's own
#    coverage-write API -- confirmed by reading its full exit path: even a
#    run that completes cleanly (reaches Verilated::gotFinish()) produces no
#    coverage.dat, because nothing here ever asks for one. Guarded by
#    VM_COVERAGE (Verilator's own auto-define when --coverage was used at
#    Verilate time), so this is a no-op for every non-coverage build --
#    mirrors the file's own existing VERILATOR_VCD guard convention right
#    next to each insertion point.
(
    cd "$ROOT"
    MY_TOP_CPP="piton/tools/verilator/my_top.cpp"
    if [ ! -f "$MY_TOP_CPP" ]; then
        echo "not found, skipping fix 5: $MY_TOP_CPP"
    elif grep -q "VM_COVERAGE" "$MY_TOP_CPP"; then
        echo "already patched: $MY_TOP_CPP"
    else
        # Same Windows-checkout CRLF root cause as fix 4, a symptom fix 4
        # never caught since its own sweep only looks at *.py/*.sh shebang
        # lines -- my_top.cpp is a plain .cpp file with no shebang, so this
        # went uncaught until a line-anchored patch (below) needed exact
        # end-of-line matches. Harmless no-op on an already-LF file.
        sed -i 's/\r$//' "$MY_TOP_CPP"

        inc_count=$(grep -c '^#include "verilated_vcd_c.h"$' "$MY_TOP_CPP")
        exit_count=$(grep -c '^delete top;$' "$MY_TOP_CPP")
        if [ "$inc_count" -ne 1 ] || [ "$exit_count" -ne 1 ]; then
            echo "my_top.cpp: expected exactly one match for each coverage-patch anchor, found inc=$inc_count exit=$exit_count -- not patching" >&2
            exit 1
        fi
        inc_line=$(grep -n '^#include "verilated_vcd_c.h"$' "$MY_TOP_CPP" | cut -d: -f1)
        exit_line=$(grep -n '^delete top;$' "$MY_TOP_CPP" | cut -d: -f1)

        # exit-path block first (higher line number) so its own insertion
        # doesn't shift the include-block's already-computed line number.
        # Each sed targets the same original line, inserted in reverse
        # desired order, so repeated single-line inserts (no fragile
        # multi-line sed escaping) stack into the right final order.
        sed -i "${exit_line}i #endif" "$MY_TOP_CPP"
        sed -i "${exit_line}i VerilatedCov::write(\"coverage.dat\");" "$MY_TOP_CPP"
        sed -i "${exit_line}i #ifdef VM_COVERAGE" "$MY_TOP_CPP"

        inc_endif_line=$((inc_line + 1))
        sed -i "${inc_endif_line}a #endif" "$MY_TOP_CPP"
        sed -i "${inc_endif_line}a #include \"verilated_cov.h\"" "$MY_TOP_CPP"
        sed -i "${inc_endif_line}a #ifdef VM_COVERAGE" "$MY_TOP_CPP"

        echo "patched: my_top.cpp writes coverage.dat when VM_COVERAGE is defined"
    fi
)
