#!/bin/bash
# Build and run generic_bridge_wishbone_tb with Verilator.
#
# Usage: scripts/generic_ut/run_bridge_wishbone_ut.sh <piton_root> [build_dir]
#
# The bridges use OpenPiton's generated include files (define.tmp.h and
# l15.tmp.h), which exist once the checkout has built any manycore model.
# RTL_DIR overrides the directory the bridges are read from (the mutation run
# uses it). Exits 0 when the testbench prints PASS.
set -euo pipefail

ROOT="${1:?usage: $0 <piton_root> [build_dir]}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RTL="${RTL_DIR:-$HERE/../openpiton_generic/piton/design/chip/tile/generic/rtl}"
INC="$ROOT/piton/design/include"
OUT="${2:-$(mktemp -d)}"
VERILATOR="${VERILATOR:-verilator}"

for h in define.tmp.h l15.tmp.h iop.h; do
    if [ ! -f "$INC/$h" ]; then
        echo "not found: $INC/$h (build a manycore model in this checkout first)" >&2
        exit 2
    fi
done

mkdir -p "$OUT"
cat > "$OUT/prelude.sv" <<'EOF'
`include "define.tmp.h"
`include "l15.tmp.h"
`include "iop.h"
EOF

"$VERILATOR" --binary --timing -Wno-fatal -Wno-lint -Wno-style \
    -I"$INC" --top-module generic_bridge_wishbone_tb -Mdir "$OUT/obj_dir" \
    "$OUT/prelude.sv" "$RTL/generic_bridge_wishbone.v" "$RTL/generic_bridge_wishbone2.v" \
    "$HERE/generic_bridge_wishbone_tb.sv" \
    > "$OUT/build.log" 2>&1 || { tail -30 "$OUT/build.log"; exit 1; }

"$OUT/obj_dir/Vgeneric_bridge_wishbone_tb" | tee "$OUT/run.log"
grep -q "^PASS: generic_bridge_wishbone_tb" "$OUT/run.log"
