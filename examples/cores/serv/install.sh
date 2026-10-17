#!/bin/bash
# Copy SERV's RTL into an OpenPiton checkout for the generic tile, then write
# its Flist.core, link.ld, and a wrapper skeleton from examples/cores/serv.yaml.
# The wrapper itself is MACE's to write.
#
# Usage: examples/cores/serv/install.sh <serv_clone> <piton_root>
#
# The checkout must already be patched with scripts/patch_openpiton.sh
# (fixes 14-19). SERV is ISC-licensed; its LICENSE is copied beside its RTL.
set -euo pipefail

SERV="${1:?usage: $0 <serv_clone> <piton_root>}"
ROOT="${2:?usage: $0 <serv_clone> <piton_root>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
DEST="$ROOT/piton/design/chip/tile/serv"

mkdir -p "$DEST/rtl"
cp "$SERV"/rtl/*.v "$DEST/rtl/"
cp "$SERV/LICENSE" "$DEST/LICENSE"
echo "copied SERV $(git -C "$SERV" log -1 --format=%h 2>/dev/null || echo '(unknown revision)') to $DEST"

# From $REPO, so python -m imports this checkout's chia_openpiton.
(cd "$REPO" && "${PYTHON:-python3}" -m chia_openpiton.core_descriptor apply examples/cores/serv.yaml "$ROOT")
