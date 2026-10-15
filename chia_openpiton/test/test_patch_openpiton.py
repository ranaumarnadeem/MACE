"""Tier-0 tests for scripts/patch_openpiton.sh's RTL/testbench fixes: 6 and
7 (PicoRV32's first Verilator PASS) and 10-13 (the multi-tile Ariane bugs
behind the 2x2/4x4 passes).

These are real, exact-match text-block replacements applied to real
OpenPiton source files by a Python heredoc inside the shell script -- never
exercised by CI, only ever run against a real, multi-gigabyte OpenPiton
checkout. A future whitespace drift in the upstream files would only be
discovered the next time someone actually runs this against a fresh
checkout -- and separately, nothing catches an accidental edit to the
script's own `old`/`new` blocks either.

PICO_RESETN_OLD/PC_CMP_ACTIVE_THREAD_OLD below are a deliberate, frozen
snapshot of the exact text the script's own triple-quoted `old` blocks
contained when this test was written (copied by hand, not extracted from
the script) -- so if a future edit to patch_openpiton.sh's own old/new
blocks doesn't match this snapshot anymore, this test fails and a human
has to consciously decide whether the snapshot needs updating too, instead
of the drift going unnoticed until a real checkout is patched. This can
only catch a change to the *script*, not a change to real upstream
OpenPiton -- that half is inherently untestable without a live checkout.

Run:
    pytest chia_openpiton/test/test_patch_openpiton.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "patch_openpiton.sh"

# Fix 6: picorv32.v's resetn/booted self-boot gate.
PICO_RESETN_OLD = """    reg        resetn;

    always @ (posedge clk) begin
        if(!reset_l) begin
            resetn <= 1'b0;
        end
        else if (mem_la_write & (mem_la_addr == 32'hffffffff)) begin
            resetn <= 1'b0;
        end
        else if (pico_int) begin
            resetn <= 1'b1;
        end
    end"""
PICO_RESETN_NEW = """    reg        resetn;
    reg        booted;

    always @ (posedge clk) begin
        if(!reset_l) begin
            resetn <= 1'b0;
            booted <= 1'b0;
        end
        else if (mem_la_write & (mem_la_addr == 32'hffffffff)) begin
            resetn <= 1'b0;
        end
        else if (pico_int || !booted) begin
            resetn <= 1'b1;
            booted <= 1'b1;
        end
    end"""

# Fix 7: pc_cmp.v.pyv's RTL_PICO0 active_thread tracking.
PC_CMP_ACTIVE_THREAD_OLD = """                always @*
                begin
                    if (`PICO_CORE0.pico_int == 1'b1)
                    begin
                        active_thread[(0*4)] = 1'b1;
                        active_thread[(0*4)+1] = 1'b1;
                        active_thread[(0*4)+2] = 1'b1;
                        active_thread[(0*4)+3] = 1'b1;
                    end
                end"""
PC_CMP_ACTIVE_THREAD_NEW = """                always @(posedge clk) begin
                    if (~rst_l) begin
                      active_thread[(0*4)]   <= 1'b0;
                      active_thread[(0*4)+1] <= 1'b0;
                      active_thread[(0*4)+2] <= 1'b0;
                      active_thread[(0*4)+3] <= 1'b0;
                    end else begin
                      active_thread[(0*4)]   <= 1'b1;
                      active_thread[(0*4)+1] <= 1'b1;
                      active_thread[(0*4)+2] <= 1'b1;
                      active_thread[(0*4)+3] <= 1'b1;
                    end
                end"""


# Fix 10: pc_cmp.v.pyv's Verilator-only 32-bit finish_mask.
FINISH_MASK_OLD = """    `ifndef VERILATOR
    reg [31:0]   finish_mask;
    `else
    integer      finish_mask;
    `endif
"""
FINISH_MASK_NEW = """    reg [31:0]   finish_mask;
"""

# Fix 11: syscalls.c's plain-load exit barrier.
SYSCALLS_OLD = ("  while(finish_sync0 != nc);", "  while(finish_sync1 != cid);")
SYSCALLS_NEW = (
    "  { uint32_t v; do { ATOMIC_FETCH_OP(v, finish_sync0, 0, add, w); } while (v != nc); }",
    "  { uint32_t v; do { ATOMIC_FETCH_OP(v, finish_sync1, 0, add, w); } while (v != cid); }",
)

# Fix 12: cva6.sv's hardcoded tracer filename.
CVA6_TRACE_OLD = """    f = $fopen("trace_hart_00.dasm", "w");"""
CVA6_TRACE_NEW = """    string dasm_fn;
    $sformat(dasm_fn, "trace_hart_%0.0f.dasm", hart_id_i);
    f = $fopen(dasm_fn, "w");"""

# Fix 13: wt_l15_adapter.sv drives the invalidation fields from continuous
# assigns; cva6#2809 moves them into p_rtrn_logic.
ADAPTER_DEFAULTS = """  always_comb begin : p_rtrn_logic
    icache_rtrn_o.rtype = ICACHE_IFILL_ACK;
    dcache_rtrn_o.rtype = DCACHE_LOAD_ACK;
    icache_rtrn_vld_o   = 1'b0;
    dcache_rtrn_vld_o   = 1'b0;
"""
ADAPTER_INV_ASSIGNS = (
    """  assign icache_rtrn_o.inv.vld  = rtrn_fifo_data.l15_inval_icache_inval;
  assign icache_rtrn_o.inv.all  = rtrn_fifo_data.l15_inval_icache_all_way;
""",
    """  assign dcache_rtrn_o.inv.vld  = rtrn_fifo_data.l15_inval_dcache_inval;
  assign dcache_rtrn_o.inv.all  = rtrn_fifo_data.l15_inval_dcache_all_way;
""",
)
ADAPTER_INV_IN_BLOCK = """    icache_rtrn_o.inv.vld = rtrn_fifo_data.l15_inval_icache_inval;
    icache_rtrn_o.inv.all = rtrn_fifo_data.l15_inval_icache_all_way;
    dcache_rtrn_o.inv.vld = rtrn_fifo_data.l15_inval_dcache_inval;
    dcache_rtrn_o.inv.all = rtrn_fifo_data.l15_inval_dcache_all_way;
"""
ADAPTER_INV_IDX = (
    "  assign icache_rtrn_o.inv.idx  = {rtrn_fifo_data.l15_inval_address_15_4, 4'b0000};\n",
    "  assign dcache_rtrn_o.inv.idx  = {rtrn_fifo_data.l15_inval_address_15_4, 4'b0000};\n",
)

PC_CMP = "piton/verif/env/manycore/pc_cmp.v.pyv"
SYSCALLS = "piton/verif/diag/assembly/include/riscv/ariane/syscalls.c"
CVA6 = "piton/design/chip/tile/ariane/core/cva6.sv"
ADAPTER = "piton/design/chip/tile/ariane/core/cache_subsystem/wt_l15_adapter.sv"


def _run_patch(piton_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(piton_root)],
        capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def synthetic_piton_root(tmp_path) -> Path:
    """A minimal OpenPiton-shaped tree with just enough real structure for
    patch_openpiton.sh to run to completion: a real git repo (so fix 3's
    symlink check has a real, empty answer instead of an ambiguous
    non-repo one), a bootrom Makefile (the script's own precondition
    check), and piton/tools/src/sims/ (the pico_reset_ut generation step
    unconditionally writes a new .config file there, with no "not found,
    skipping" guard the way fixes 5/8 do for their own target files).
    Every fix whose target file isn't created here (3, 4, 5, 8) takes its
    own documented "not found, skipping" path -- this fixture only
    populates what fixes 1, 2, 6, 7, and the pico_reset_ut generation
    actually need.
    """
    root = tmp_path / "openpiton"
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    bootrom_mk = root / "piton" / "design" / "chipset" / "rv64_platform" / "bootrom" / "linux"
    bootrom_mk.mkdir(parents=True)
    (bootrom_mk / "Makefile").write_text("CFLAGS = -march=rv64imac -mabi=lp64\n")

    (root / "piton" / "tools" / "src" / "sims").mkdir(parents=True)

    pico_rtl = root / "piton" / "design" / "chip" / "tile" / "pico" / "rtl"
    pico_rtl.mkdir(parents=True)
    (pico_rtl / "picorv32.v").write_text(
        f"module picorv32 (\n    input clk,\n    input reset_l\n);\n{PICO_RESETN_OLD}\nendmodule\n"
    )

    pc_cmp_dir = root / "piton" / "verif" / "env" / "manycore"
    pc_cmp_dir.mkdir(parents=True)
    (pc_cmp_dir / "pc_cmp.v.pyv").write_text(
        f"module manycore_monitor;\n{PC_CMP_ACTIVE_THREAD_OLD}\nendmodule\n"
    )
    return root


class TestPicoFixesApplyToASyntheticTree:
    def test_fix_6_replaces_picorv32s_resetn_gate(self, synthetic_piton_root):
        result = _run_patch(synthetic_piton_root)
        assert result.returncode == 0, result.stderr

        text = (
            synthetic_piton_root / "piton/design/chip/tile/pico/rtl/picorv32.v"
        ).read_text()
        assert PICO_RESETN_OLD not in text
        assert PICO_RESETN_NEW in text
        assert "pico_int || !booted" in text

    def test_fix_7_replaces_pc_cmps_active_thread_block(self, synthetic_piton_root):
        result = _run_patch(synthetic_piton_root)
        assert result.returncode == 0, result.stderr

        text = (
            synthetic_piton_root / "piton/verif/env/manycore/pc_cmp.v.pyv"
        ).read_text()
        assert PC_CMP_ACTIVE_THREAD_OLD not in text
        assert PC_CMP_ACTIVE_THREAD_NEW in text
        # fix 7's own detection logic for "already patched" -- see the
        # script's comment on why absence, not presence, is the real signal.
        assert "PICO_CORE0.pico_int" not in text

    def test_both_fixes_are_idempotent(self, synthetic_piton_root):
        first = _run_patch(synthetic_piton_root)
        assert first.returncode == 0, first.stderr

        pico_after_first = (
            synthetic_piton_root / "piton/design/chip/tile/pico/rtl/picorv32.v"
        ).read_text()
        pc_cmp_after_first = (
            synthetic_piton_root / "piton/verif/env/manycore/pc_cmp.v.pyv"
        ).read_text()

        second = _run_patch(synthetic_piton_root)
        assert second.returncode == 0, second.stderr
        assert "already patched" in second.stdout

        assert (
            synthetic_piton_root / "piton/design/chip/tile/pico/rtl/picorv32.v"
        ).read_text() == pico_after_first
        assert (
            synthetic_piton_root / "piton/verif/env/manycore/pc_cmp.v.pyv"
        ).read_text() == pc_cmp_after_first


@pytest.fixture
def multitile_piton_root(synthetic_piton_root) -> Path:
    """synthetic_piton_root plus the upstream text fixes 10-13 target."""
    root = synthetic_piton_root
    (root / PC_CMP).write_text(
        "module manycore_monitor;\n"
        f"{FINISH_MASK_OLD}"
        f"{PC_CMP_ACTIVE_THREAD_OLD}\n"
        "endmodule\n"
    )
    (root / SYSCALLS).parent.mkdir(parents=True)
    (root / SYSCALLS).write_text(
        "void _init(int cid, int nc)\n{\n"
        f"{SYSCALLS_OLD[0]}\n{SYSCALLS_OLD[1]}\n"
        "}\n"
    )
    (root / CVA6).parent.mkdir(parents=True)
    (root / CVA6).write_text(f"  initial begin\n{CVA6_TRACE_OLD}\n  end\n")
    (root / ADAPTER).parent.mkdir(parents=True)
    (root / ADAPTER).write_text(
        f"module wt_l15_adapter;\n{ADAPTER_DEFAULTS}    if(!rtrn_fifo_empty) begin\n    end\n  end\n\n"
        f"{ADAPTER_INV_IDX[0]}{ADAPTER_INV_ASSIGNS[0]}\n{ADAPTER_INV_IDX[1]}{ADAPTER_INV_ASSIGNS[1]}endmodule\n"
    )
    return root


class TestMultiTileFixesApplyToASyntheticTree:
    def test_fix_10_makes_finish_mask_a_widenable_reg(self, multitile_piton_root):
        result = _run_patch(multitile_piton_root)
        assert result.returncode == 0, result.stderr

        text = (multitile_piton_root / PC_CMP).read_text()
        assert FINISH_MASK_OLD not in text
        assert FINISH_MASK_NEW in text
        assert "integer      finish_mask;" not in text

    def test_fix_11_polls_the_exit_barrier_atomically(self, multitile_piton_root):
        result = _run_patch(multitile_piton_root)
        assert result.returncode == 0, result.stderr

        text = (multitile_piton_root / SYSCALLS).read_text()
        for old, new in zip(SYSCALLS_OLD, SYSCALLS_NEW):
            assert old not in text
            assert new in text

    def test_fix_12_names_the_tracer_file_per_hart(self, multitile_piton_root):
        result = _run_patch(multitile_piton_root)
        assert result.returncode == 0, result.stderr

        text = (multitile_piton_root / CVA6).read_text()
        assert CVA6_TRACE_OLD not in text
        assert CVA6_TRACE_NEW in text

    def test_fix_13_moves_the_invalidation_fields_into_p_rtrn_logic(self, multitile_piton_root):
        result = _run_patch(multitile_piton_root)
        assert result.returncode == 0, result.stderr

        text = (multitile_piton_root / ADAPTER).read_text()
        assert ADAPTER_DEFAULTS + ADAPTER_INV_IN_BLOCK in text
        for assigns in ADAPTER_INV_ASSIGNS:
            assert assigns not in text
        for idx in ADAPTER_INV_IDX:
            assert idx in text

    def test_fixes_10_to_13_are_idempotent(self, multitile_piton_root):
        first = _run_patch(multitile_piton_root)
        assert first.returncode == 0, first.stderr
        after_first = {p: (multitile_piton_root / p).read_text() for p in (PC_CMP, SYSCALLS, CVA6, ADAPTER)}

        second = _run_patch(multitile_piton_root)
        assert second.returncode == 0, second.stderr
        for fix in ("fix 10", "fix 11", "fix 12", "fix 13"):
            assert f"({fix})" in second.stdout, fix
        for p, text in after_first.items():
            assert (multitile_piton_root / p).read_text() == text


def _run_patch_skipping(piton_root: Path, skip: str) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(
        ["bash", str(SCRIPT), str(piton_root)],
        capture_output=True, text=True, timeout=60, env={**os.environ, "PATCH_SKIP": skip},
    )


class TestPatchSkip:
    """PATCH_SKIP leaves the named fixes out and applies every other one, so
    an evaluation can reproduce the failure one fix addresses."""

    def test_a_skipped_fix_leaves_its_file_alone(self, multitile_piton_root):
        before = (multitile_piton_root / SYSCALLS).read_text()
        result = _run_patch_skipping(multitile_piton_root, "11")
        assert result.returncode == 0, result.stderr
        assert "skipped fix 11 (PATCH_SKIP)" in result.stdout
        assert (multitile_piton_root / SYSCALLS).read_text() == before
        assert FINISH_MASK_NEW in (multitile_piton_root / PC_CMP).read_text()
        assert CVA6_TRACE_NEW in (multitile_piton_root / CVA6).read_text()

    def test_several_fixes_can_be_skipped(self, multitile_piton_root):
        result = _run_patch_skipping(multitile_piton_root, "10 12")
        assert result.returncode == 0, result.stderr
        assert FINISH_MASK_OLD in (multitile_piton_root / PC_CMP).read_text()
        assert CVA6_TRACE_OLD in (multitile_piton_root / CVA6).read_text()
        for new in SYSCALLS_NEW:
            assert new in (multitile_piton_root / SYSCALLS).read_text()

    def test_skipping_fix_13_keeps_the_adapter_s_assigns(self, multitile_piton_root):
        before = (multitile_piton_root / ADAPTER).read_text()
        result = _run_patch_skipping(multitile_piton_root, "13")
        assert result.returncode == 0, result.stderr
        assert (multitile_piton_root / ADAPTER).read_text() == before

    def test_skipping_the_boot_rom_fixes_leaves_its_makefile_alone(self, synthetic_piton_root):
        makefile = synthetic_piton_root / "piton/design/chipset/rv64_platform/bootrom/linux/Makefile"
        before = makefile.read_text()
        result = _run_patch_skipping(synthetic_piton_root, "1 2")
        assert result.returncode == 0, result.stderr
        assert makefile.read_text() == before
        assert "skipped fix 1 (PATCH_SKIP)" in result.stdout
        assert "skipped fix 2 (PATCH_SKIP)" in result.stdout

    def test_without_patch_skip_nothing_is_skipped(self, multitile_piton_root):
        result = _run_patch(multitile_piton_root)
        assert "skipped fix" not in result.stdout


# Fixes 14-17: the generic tile. Upstream text each fix anchors on, as in
# OpenPiton 1c6bfd2.
GENERIC_SRC = SCRIPT.parent / "openpiton_generic" / "piton"
DEFINE_H = "piton/design/include/define.h.pyv"
TILE_V = "piton/design/chip/tile/rtl/tile.v.pyv"
CHIP_V = "piton/design/chip/rtl/chip.v.pyv"
PYHPLIB = "piton/tools/bin/pyhplib.py"
CROSS_MODULE = "piton/verif/env/manycore/cross_module.h.pyv"
SIMS_PL = "piton/tools/src/sims/sims,2.0"
MANYCORE_CONFIG = "piton/tools/src/sims/manycore.config"
RV32_AS = "piton/tools/bin/rv32_as"
PICO_MACROS = "piton/verif/diag/assembly/include/riscv/pico/test_macros.h"
GENERIC_INCLUDE = "piton/verif/diag/assembly/include/riscv/generic"
FLIST_CORE = "piton/design/chip/tile/generic/rtl/Flist.core"

UPSTREAM = {
    DEFINE_H: "`define SPARC_TILE       0\n`define PICORV32_TILE    1\n`define ARIANE_RV64_TILE 2\n",
    TILE_V: (
        "generate\nif (TILE_TYPE == `PICORV32_TILE) begin : g_picorv32_core\n"
        "`ifdef PITON_PICO\n`endif // ifdef PITON_PICO\nend\nendgenerate\n"
    ),
    CHIP_V: (
        "            elif (PITON_PICO):\n"
        "                currenttype = \"`PICORV32_TILE\"\n"
    ),
    PYHPLIB: (
        "PITON_PICO_HET = int(os.environ.get('PITON_PICO_HET', '0'))\n"
        "if PITON_ARIANE or PITON_PICO:\n    NUM_THREADS = PITON_NUM_TILES\n"
    ),
    CROSS_MODULE: "    `define PICO_TRANSDUCER%d `TILE%d.g_picorv32_core.pico_l15_transducer\n",
    SIMS_PL: (
        "        'pico' => 0,\n"
        "            'pico!',\n"
        'die ("DIE. -pico and -pico_het cannot both be set") if ($opt{pico} && $opt{pico_het}) ;\n'
        "if (!$opt{pico} && !$opt{ariane}) {\n"
        'push (@{$opt{config_cpp_args}}, "-DFLIST_ARIANE")   if ($opt{ariane}) ;\n'
        "  $ENV{PITON_ARIANE}   = $opt{ariane};\n"
        "    if ($opt{pico} || ($opt{pico_het} && ($i % 2 == 1))) {\n"
        + "        if ($opt{pico}) {\n" * 3
        + '      my $cmd = "rv32_as " ;\n'
    ),
    MANYCORE_CONFIG: (
        "#ifdef FLIST_PICO\n    -flist=$DV_ROOT/design/chip/tile/pico/rtl/Flist.pico\n"
        "    -rv32\n    -config_rtl=PITON_PICO\n#endif\n"
    ),
    RV32_AS: (
        "-I${DV_ROOT}/verif/diag/assembly/include/riscv/pico  \\\n"
        "-T${DV_ROOT}/verif/diag/assembly/include/riscv/pico/link.ld diag.S -o diag.exe\n"
    ),
    PICO_MACROS: "#define TEST_PASSFAIL\n",
}
PC_CMP_GENERIC_ANCHORS = (
    "        `else // RTL_ARIANE0\n        `ifdef RTL_PICO0\n"
    "        `endif // RTL_PICO0\n        `endif // RTL_ARIANE0\n"
    '    pattern = ["SPARC0", "PICO_CORE0", "ARIANE_CORE0", "active_thread[(0"];\n'
)


@pytest.fixture
def generic_piton_root(multitile_piton_root) -> Path:
    """multitile_piton_root plus the upstream text fixes 14-17 target. Runs
    leave out fixes 8 and 9, whose own sims,2.0 anchors this tree lacks."""
    root = multitile_piton_root
    for rel, text in UPSTREAM.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    pc_cmp = root / PC_CMP
    pc_cmp.write_text(pc_cmp.read_text().replace("endmodule\n", PC_CMP_GENERIC_ANCHORS + "endmodule\n"))
    return root


class TestGenericTileFixes:
    def test_fix_14_installs_the_generic_tile_files(self, generic_piton_root):
        result = _run_patch_skipping(generic_piton_root, "8 9")
        assert result.returncode == 0, result.stderr
        sources = [p for p in GENERIC_SRC.rglob("*") if p.is_file()]
        assert sources
        for src in sources:
            installed = generic_piton_root / "piton" / src.relative_to(GENERIC_SRC)
            assert installed.read_bytes() == src.read_bytes(), installed
        assert (generic_piton_root / GENERIC_INCLUDE / "test_macros.h").read_text() == UPSTREAM[PICO_MACROS]

    def test_fix_14_keeps_a_descriptor_s_flist_core(self, generic_piton_root):
        flist = generic_piton_root / FLIST_CORE
        flist.parent.mkdir(parents=True)
        flist.write_text("../../ibex/rtl/generic_core_wrapper.sv\n")
        result = _run_patch_skipping(generic_piton_root, "8 9")
        assert result.returncode == 0, result.stderr
        assert flist.read_text() == "../../ibex/rtl/generic_core_wrapper.sv\n"

    def test_fix_14_restores_an_edited_transducer(self, generic_piton_root):
        _run_patch_skipping(generic_piton_root, "8 9")
        rel = "design/chip/tile/generic/rtl/generic_l15_transducer.v"
        (generic_piton_root / "piton" / rel).write_text("// edited\n")
        result = _run_patch_skipping(generic_piton_root, "8 9")
        assert result.returncode == 0, result.stderr
        assert f"installed: piton/{rel}" in result.stdout
        assert (generic_piton_root / "piton" / rel).read_bytes() == (GENERIC_SRC / rel).read_bytes()

    def test_fix_15_adds_the_generic_tile_type(self, generic_piton_root):
        result = _run_patch_skipping(generic_piton_root, "8 9")
        assert result.returncode == 0, result.stderr
        assert "`define GENERIC_TILE     3\n" in (generic_piton_root / DEFINE_H).read_text()
        tile = (generic_piton_root / TILE_V).read_text()
        assert "if (TILE_TYPE == `GENERIC_TILE) begin : g_generic_core" in tile
        assert "generic_l15_transducer #(" in tile
        assert tile.index("g_picorv32_core") < tile.index("g_generic_core")
        chip = (generic_piton_root / CHIP_V).read_text()
        assert chip.index("elif (PITON_GENERIC):") < chip.index("elif (PITON_PICO):")
        pyhplib = (generic_piton_root / PYHPLIB).read_text()
        assert "PITON_GENERIC  = int(os.environ.get('PITON_GENERIC', '0'))" in pyhplib
        assert "if PITON_ARIANE or PITON_PICO or PITON_GENERIC:" in pyhplib
        cross = (generic_piton_root / CROSS_MODULE).read_text()
        assert "`define GENERIC_TRANSDUCER%d `TILE%d.g_generic_core.generic_l15_transducer" in cross

    def test_fix_16_adds_the_rtl_generic_branch_to_pc_cmp(self, generic_piton_root):
        result = _run_patch_skipping(generic_piton_root, "8 9")
        assert result.returncode == 0, result.stderr
        text = (generic_piton_root / PC_CMP).read_text()
        assert text.index("`ifdef RTL_GENERIC0") < text.index("`ifdef RTL_PICO0")
        assert "`endif // RTL_PICO0\n        `endif // RTL_GENERIC0\n        `endif // RTL_ARIANE0\n" in text
        assert "{24'b0, good_trap[0]} : {24'b0, bad_trap[0]}" in text
        assert '"RTL_GENERIC0", "GENERIC_TRANSDUCER0"]' in text

    def test_fix_17_adds_sims_generic(self, generic_piton_root):
        result = _run_patch_skipping(generic_piton_root, "8 9")
        assert result.returncode == 0, result.stderr
        sims = (generic_piton_root / SIMS_PL).read_text()
        for line in (
            "        'generic' => 0,\n",
            "            'generic!',\n",
            "if (!$opt{pico} && !$opt{ariane} && !$opt{generic}) {\n",
            'push (@{$opt{config_cpp_args}}, "-DFLIST_GENERIC")  if ($opt{generic}) ;\n',
            "  $ENV{PITON_GENERIC}  = $opt{generic};\n",
            '        push (@{$opt{config_rtl}}, "RTL_GENERIC" . $i);\n',
            '      $ENV{RV32_ENV} = $opt{generic} ? "generic" : "pico" ;\n',
        ):
            assert line in sims, line
        assert sims.count("        if ($opt{pico} || $opt{generic}) {\n") == 3
        config = (generic_piton_root / MANYCORE_CONFIG).read_text()
        assert "#ifdef FLIST_GENERIC\n" in config
        assert "    -config_rtl=PITON_GENERIC\n" in config
        rv32_as = (generic_piton_root / RV32_AS).read_text()
        assert "include/riscv/pico" not in rv32_as
        assert rv32_as.count("include/riscv/${RV32_ENV:-pico}") == 2

    def test_fixes_14_to_17_are_idempotent(self, generic_piton_root):
        first = _run_patch_skipping(generic_piton_root, "8 9")
        assert first.returncode == 0, first.stderr
        paths = [*UPSTREAM, PC_CMP]
        after_first = {p: (generic_piton_root / p).read_text() for p in paths}
        second = _run_patch_skipping(generic_piton_root, "8 9")
        assert second.returncode == 0, second.stderr
        for fix in ("fix 14", "fix 15", "fix 16", "fix 17"):
            assert f"({fix})" in second.stdout, fix
        assert "installed:" not in second.stdout
        for p, text in after_first.items():
            assert (generic_piton_root / p).read_text() == text, p

    def test_skipping_fix_17_leaves_sims_alone(self, generic_piton_root):
        result = _run_patch_skipping(generic_piton_root, "8 9 17")
        assert result.returncode == 0, result.stderr
        assert (generic_piton_root / SIMS_PL).read_text() == UPSTREAM[SIMS_PL]
        assert (generic_piton_root / RV32_AS).read_text() == UPSTREAM[RV32_AS]
        assert "GENERIC_TILE" in (generic_piton_root / DEFINE_H).read_text()

    def test_a_changed_anchor_fails_loudly(self, generic_piton_root):
        (generic_piton_root / CHIP_V).write_text("            elif (PITON_PICO) :\n")
        result = _run_patch_skipping(generic_piton_root, "8 9")
        assert result.returncode != 0
        assert "chip.v.pyv: expected exactly 1 match" in result.stderr
