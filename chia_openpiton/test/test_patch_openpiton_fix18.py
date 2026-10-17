"""Tier-0 tests for scripts/patch_openpiton.sh fix 18: sims -rv32_image_base,
which exports RV32_IMAGE_BASE for rv32_as. The fixtures are those of
test_patch_openpiton.py.

Run:
    pytest chia_openpiton/test/test_patch_openpiton_fix18.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# The test directory has no __init__.py, so load the sibling module by path.
_PATH = Path(__file__).with_name("test_patch_openpiton.py")
_SPEC = importlib.util.spec_from_file_location("_patch_openpiton_fixtures", _PATH)
base = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = base
_SPEC.loader.exec_module(base)

synthetic_piton_root = base.synthetic_piton_root
multitile_piton_root = base.multitile_piton_root
generic_piton_root = base.generic_piton_root

SIMS = base.SIMS_PL
OPTION_DEFAULT = "        'rv32_mabi=s' => \"\",\n"
OPTION_LIST = "            'rv32_mabi=s',\n"
HELP = "    -rv32_mabi\n           specifies rv32 mabi string (e.g. ilp32)\n           default varies by core\n"


@pytest.fixture
def sims_root(generic_piton_root) -> Path:
    """generic_piton_root with the -rv32_mabi option text fix 18 anchors on."""
    sims = generic_piton_root / SIMS
    sims.write_text(sims.read_text() + OPTION_DEFAULT + OPTION_LIST + HELP)
    return generic_piton_root


def patch(root: Path, skip: str = "8 9"):
    return base._run_patch_skipping(root, skip)


class TestFix18:
    def test_it_adds_the_option_and_exports_the_variable(self, sims_root):
        result = patch(sims_root)
        assert result.returncode == 0, result.stderr
        assert f"patched: {SIMS} takes -rv32_image_base" in result.stdout
        sims = (sims_root / SIMS).read_text()
        assert "'rv32_image_base=s' => " not in sims
        assert OPTION_LIST + "            'rv32_image_base=s',\n" in sims
        assert "      $ENV{RV32_IMAGE_BASE} = $opt{rv32_image_base} ;\n      my $cmd = \"rv32_as \" ;\n" in sims
        assert "    -rv32_image_base\n           address rv32_as loads the program image at" in sims

    def test_the_default_is_set_where_the_option_is_used(self, sims_root):
        """sims' %opt defaults do not survive argument parsing, so an omitted option is undef."""
        patch(sims_root)
        sims = (sims_root / SIMS).read_text()
        default = '$opt{rv32_image_base} = "0000000040000000" unless ($opt{rv32_image_base}) ;'
        assert sims.index(default) < sims.index("-rv32_image_base must be 16 hex digits")

    def test_the_value_is_checked_before_rv32_as_runs(self, sims_root):
        patch(sims_root)
        sims = (sims_root / SIMS).read_text()
        check = sims.index("-rv32_image_base must be 16 hex digits")
        assert "[0-9a-fA-F]{16}" in sims[check:check + 200]
        assert check < sims.index("$ENV{RV32_IMAGE_BASE}") < sims.index('my $cmd = "rv32_as "')

    def test_it_is_idempotent(self, sims_root):
        assert patch(sims_root).returncode == 0
        after_first = (sims_root / SIMS).read_text()
        second = patch(sims_root)
        assert second.returncode == 0, second.stderr
        assert f"already patched: {SIMS} (fix 18)" in second.stdout
        assert (sims_root / SIMS).read_text() == after_first
        assert after_first.count("rv32_image_base=s") == 1

    def test_it_works_before_or_after_fix_17(self, sims_root):
        patch(sims_root, "8 9 17")
        assert patch(sims_root).returncode == 0
        sims = (sims_root / SIMS).read_text()
        assert "$ENV{RV32_IMAGE_BASE}" in sims and '$ENV{RV32_ENV} = $opt{generic}' in sims

    def test_patch_skip_leaves_sims_without_the_option(self, sims_root):
        result = patch(sims_root, "8 9 18")
        assert result.returncode == 0, result.stderr
        assert "skipped fix 18 (PATCH_SKIP)" in result.stdout
        assert "rv32_image_base" not in (sims_root / SIMS).read_text()

    def test_a_sims_without_rv32_mabi_is_skipped(self, generic_piton_root):
        result = patch(generic_piton_root)
        assert result.returncode == 0, result.stderr
        assert "skipping fix 18 (no -rv32_mabi option)" in result.stdout
        assert "rv32_image_base" not in (generic_piton_root / SIMS).read_text()

    def test_a_changed_anchor_fails_loudly(self, sims_root):
        sims = sims_root / SIMS
        sims.write_text(sims.read_text().replace(OPTION_LIST, "            'rv32_mabi=s' ,\n"))
        result = patch(sims_root)
        assert result.returncode != 0
        assert "expected exactly 1 match" in result.stderr
        assert "rv32_image_base" not in sims.read_text()
