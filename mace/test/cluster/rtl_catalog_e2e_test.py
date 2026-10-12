"""Tier-2 tests: the RTL-parameter catalog against a patched OpenPiton checkout.

Run:
    OPENPITON_TEST_REAL=1 \
    OPENPITON_ROOT=/home/you/openpiton-c \
    pytest mace/test/cluster/rtl_catalog_e2e_test.py -q

The checkout must be at the commit the catalog was written against (1c6bfd2,
with the Ariane submodule) and must hold no leftover edits. The tests read the
checkout and write only to a temporary directory; they build nothing.

* Every edit's old text occurs exactly once at the default value, and the edit
  applies at every value the catalog allows.
* PyHP, run on an edited copy of each header the catalog edits, writes the new
  text into the generated header, which is the text a build is checked for.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mace.codesign import rtl
from mace.codesign.rtl import CATALOG, RtlDesign, edits_for

REAL = os.environ.get("OPENPITON_TEST_REAL") == "1"
ROOT = os.environ.get("OPENPITON_ROOT", "")

real_only = pytest.mark.skipif(
    not (REAL and ROOT), reason="set OPENPITON_TEST_REAL=1 and OPENPITON_ROOT to run"
)

VALUES = [(name, value) for name, param in CATALOG.items() for value in param.values if value != param.default]


@real_only
class TestCatalogAgainstTheCheckout:
    @pytest.mark.parametrize("name,value", VALUES)
    def test_the_edit_applies_at_every_allowed_value(self, name, value):
        changed = edits_for(RtlDesign.of({name: value}), rtl.read_in(ROOT))
        assert set(changed) == {e.path for e in CATALOG[name].edits}

    def test_pyhp_writes_each_default_text_once_into_the_generated_header(self, tmp_path):
        out = _pyhp(tmp_path, Path(ROOT) / "piton/design/include/l2.h.pyv")
        for param in CATALOG.values():
            for edit in param.edits:
                if edit.generated:
                    assert out.count(edit.text(param.default)) == 1, (param.name, edit.template)

    @pytest.mark.parametrize("name,value", [(n, v) for n, v in VALUES if any(e.generated for e in CATALOG[n].edits)])
    def test_pyhp_writes_the_new_value_into_the_generated_header(self, tmp_path, name, value):
        design = RtlDesign.of({name: value})
        path = "piton/design/include/l2.h.pyv"
        source = tmp_path / "l2_edited.h.pyv"
        source.write_text(edits_for(design, rtl.read_in(ROOT))[path])
        out = _pyhp(tmp_path, source)
        for generated_path, expected in design.generated_checks():
            assert generated_path == "piton/design/include/l2.tmp.h"
            assert expected in out, (name, value, expected)


def _pyhp(cwd: Path, source: Path) -> str:
    """PyHP's output for *source*, run with the checkout's settings."""
    result = subprocess.run(
        ["bash", "-c", f'source "{ROOT}/piton/piton_settings.bash" >/dev/null 2>&1 && pyhp.py "{source}"'],
        capture_output=True, text=True, cwd=cwd, env={**os.environ, "PITON_ROOT": ROOT}, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-500:]
    return result.stdout
