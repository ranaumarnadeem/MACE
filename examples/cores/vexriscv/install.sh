#!/bin/bash
# Copy VexRiscv's pre-generated Verilog into an OpenPiton checkout for the
# generic tile, then write its Flist.core, link.ld, and a wrapper skeleton from
# examples/cores/vexriscv.yaml. The wrapper itself is MACE's to write.
#
# Usage: examples/cores/vexriscv/install.sh <pythondata_cpu_vexriscv_clone> <piton_root>
#
# The clone is https://github.com/litex-hub/pythondata-cpu-vexriscv. The
# checkout must already be patched with scripts/patch_openpiton.sh (fixes
# 14-19). VexRiscv is MIT-licensed; its LICENSE is copied beside its RTL.
set -euo pipefail

VEX="${1:?usage: $0 <pythondata_cpu_vexriscv_clone> <piton_root>}"
ROOT="${2:?usage: $0 <pythondata_cpu_vexriscv_clone> <piton_root>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
DEST="$ROOT/piton/design/chip/tile/vexriscv"

mkdir -p "$DEST"
cp "$VEX/pythondata_cpu_vexriscv/verilog/VexRiscv_Min.v" "$DEST/VexRiscv_Min.v"
cp "$VEX/LICENSE" "$DEST/LICENSE"
echo "copied VexRiscv_Min $(git -C "$VEX" log -1 --format=%h 2>/dev/null || echo '(unknown revision)') to $DEST"

# From $REPO, so python -m imports this checkout's chia_openpiton.
(cd "$REPO" && "${PYTHON:-python3}" -m chia_openpiton.core_descriptor apply examples/cores/vexriscv.yaml "$ROOT")
