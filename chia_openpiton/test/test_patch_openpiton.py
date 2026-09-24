"""Tier-0 tests for scripts/patch_openpiton.sh's RTL/testbench fixes: 6 and
7 (PicoRV32's first Verilator PASS) and 10-12 (the three multi-tile Ariane
bugs behind the 2x2/4x4 passes).

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

PC_CMP = "piton/verif/env/manycore/pc_cmp.v.pyv"
SYSCALLS = "piton/verif/diag/assembly/include/riscv/ariane/syscalls.c"
CVA6 = "piton/design/chip/tile/ariane/core/cva6.sv"


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
    """synthetic_piton_root plus the upstream text fixes 10-12 target."""
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

    def test_fixes_10_to_12_are_idempotent(self, multitile_piton_root):
        first = _run_patch(multitile_piton_root)
        assert first.returncode == 0, first.stderr
        after_first = {p: (multitile_piton_root / p).read_text() for p in (PC_CMP, SYSCALLS, CVA6)}

        second = _run_patch(multitile_piton_root)
        assert second.returncode == 0, second.stderr
        for fix in ("fix 10", "fix 11", "fix 12"):
            assert f"({fix})" in second.stdout, fix
        for p, text in after_first.items():
            assert (multitile_piton_root / p).read_text() == text
