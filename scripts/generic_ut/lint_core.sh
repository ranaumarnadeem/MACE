#!/bin/bash
# Ladder step 1 for the generic tile: Verilator lint of the core named by
# tile/generic/rtl/Flist.core, its wrapper, and the generic tile's RTL, with
# generic_core_wrapper as the top. Port mismatches and width errors show up
# here in seconds, before a manycore build.
#
# Usage: scripts/generic_ut/lint_core.sh <piton_root> [log]
#
# Needs the generated include files of a checkout that has built a manycore
# model (define.tmp.h, l15.tmp.h). Exits with Verilator's status.
set -euo pipefail

ROOT="${1:?usage: $0 <piton_root> [log]}"
LOG="${2:-/dev/stdout}"
INC="$ROOT/piton/design/include"
GEN="$ROOT/piton/design/chip/tile/generic/rtl"
VERILATOR="${VERILATOR:-verilator}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

printf '`include "define.tmp.h"\n`include "l15.tmp.h"\n`include "iop.h"\n' > "$WORK/prelude.sv"

args=()
while IFS= read -r line; do
    line="${line%%//*}"
    line="$(echo "$line" | xargs)"
    [ -z "$line" ] && continue
    case "$line" in
        +incdir+*) args+=("-I$GEN/${line#+incdir+}") ;;
        *) args+=("$GEN/$line") ;;
    esac
done < "$GEN/Flist.core"

"$VERILATOR" --lint-only -Wall -Wno-fatal -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-UNUSEDPARAM \
    -Wno-PINCONNECTEMPTY -Wno-MODDUP -Wno-TIMESCALEMOD -I"$INC" --top-module generic_core_wrapper \
    "$WORK/prelude.sv" "$GEN/generic_bridge_obi.v" "$GEN/generic_bridge_wishbone.v" "${args[@]}" > "$LOG" 2>&1
