#!/bin/bash
# Copy Ibex into an OpenPiton checkout for the generic tile, then write its
# Flist.core and link.ld from examples/cores/ibex.yaml.
#
# Usage: examples/cores/ibex/install.sh <ibex_clone> <piton_root>
#
# The checkout must already be patched with scripts/patch_openpiton.sh
# (fixes 14-17). Ibex is Apache-2.0; its LICENSE is copied beside its RTL.
set -euo pipefail

IBEX="${1:?usage: $0 <ibex_clone> <piton_root>}"
ROOT="${2:?usage: $0 <ibex_clone> <piton_root>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
DEST="$ROOT/piton/design/chip/tile/ibex"

for dir in rtl vendor/lowrisc_ip/ip/prim/rtl vendor/lowrisc_ip/ip/prim_generic/rtl vendor/lowrisc_ip/dv/sv/dv_utils; do
    mkdir -p "$DEST/$dir"
    cp -r "$IBEX/$dir/." "$DEST/$dir/"
done
cp "$IBEX/LICENSE" "$DEST/LICENSE"
cp "$HERE/generic_core_wrapper.sv" "$DEST/generic_core_wrapper.sv"
echo "copied Ibex $(git -C "$IBEX" log -1 --format=%h 2>/dev/null || echo '(unknown revision)') to $DEST"

# From $REPO, so python -m imports this checkout's chia_openpiton.
(cd "$REPO" && "${PYTHON:-python3}" -m chia_openpiton.core_descriptor apply examples/cores/ibex.yaml "$ROOT")
