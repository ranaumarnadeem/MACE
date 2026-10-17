#!/bin/bash
# Install picorv32_wb on the generic tile: copy its wrapper into an OpenPiton
# checkout, then write its Flist.core and link.ld from examples/cores/picorv32_wb.yaml.
# The core itself is OpenPiton's pico/rtl/picorv32.v, already in the checkout.
#
# Usage: examples/cores/picorv32_wb/install.sh <piton_root>
#
# The checkout must already be patched with scripts/patch_openpiton.sh
# (fixes 14-17).
set -euo pipefail

ROOT="${1:?usage: $0 <piton_root>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
DEST="$ROOT/piton/design/chip/tile/picorv32_wb"

mkdir -p "$DEST"
cp "$HERE/generic_core_wrapper.v" "$DEST/generic_core_wrapper.v"
echo "copied the picorv32_wb wrapper to $DEST"

# From $REPO, so python -m imports this checkout's chia_openpiton.
(cd "$REPO" && "${PYTHON:-python3}" -m chia_openpiton.core_descriptor apply examples/cores/picorv32_wb.yaml "$ROOT")
