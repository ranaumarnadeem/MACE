"""Tier-0 tests for scripts/patch_openpiton.sh fix 19: rv32_as builds C
programs against the generic runtime and starts mem.image at RV32_IMAGE_BASE.

The patched rv32_as is run twice over: with stub compiler tools that log
their arguments, and (when riscv64-unknown-elf-gcc is installed) with the real
compiler on every program MACE's gate and the generic ladder use.

Run:
    pytest chia_openpiton/test/test_patch_openpiton_fix19.py -q
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# The fixtures live in test_patch_openpiton.py; pytest imports tests by path, so
# the directory goes on sys.path for the bare import.
sys.path.insert(0, str(Path(__file__).parent))
from test_patch_openpiton import (  # noqa: E402,F401  (fixtures are used by name)
    GENERIC_SRC,
    RV32_AS,
    SCRIPT,
    _run_patch_skipping,
    generic_piton_root,
    multitile_piton_root,
    synthetic_piton_root,
)

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]
WORKLOADS = REPO / "mace" / "workloads"
GENERIC_INCLUDE = GENERIC_SRC / "verif" / "diag" / "assembly" / "include" / "riscv" / "generic"
GENERIC_PROGRAMS = GENERIC_SRC / "verif" / "diag" / "assembly" / "riscv" / "generic"

# Interface 2 of the D2 plan: the layout crt.S needs from link.ld.
LINK_LD = """OUTPUT_ARCH( "riscv" )
ENTRY(_start)
SECTIONS
{
  . = 0x40000000;
  .text.init : { *(.text.init) }
  .text : { *(.text .text.*) }
  .rodata : { *(.rodata .rodata.* .srodata .srodata.*) }
  .data : { *(.data .data.* .sdata .sdata.*) }
  . = ALIGN(4);
  __bss_start = .;
  .bss : { *(.bss .bss.* .sbss .sbss.* COMMON) }
  . = ALIGN(4);
  __bss_end = .;
  _end = .;
  __stack_top = 0x40000000 + 0x100000;
}
"""

STUBS = {
    "riscv64-unknown-elf-gcc": (
        'echo "gcc $*" >> "$STUB_LOG"\n'
        'case "$*" in *-print-file-name=picolibc.specs*) echo "${STUB_SPECS:-picolibc.specs}"; exit 0;; esac\n'
        "touch diag.exe\n"
    ),
    "riscv64-unknown-elf-objcopy": "echo 0123456789abcdef > diag.o\n",
    "riscv64-unknown-elf-objdump": 'echo "00000010 <pass>:"; echo "00000020 <fail>:"\n',
    "xxd": 'echo "00000000: 00112233445566778899aabbccddeeff 0011223344556677 ................"\n',
}


@pytest.fixture
def rv32_as_root(generic_piton_root) -> Path:  # noqa: F811  (a fixture, used by name)
    """generic_piton_root with the whole upstream rv32_as in place of the two
    lines the fix 14-17 tests use."""
    shutil.copy(FIXTURES / "rv32_as_upstream.sh", generic_piton_root / RV32_AS)
    return generic_piton_root


def _patched(root: Path) -> subprocess.CompletedProcess:
    result = _run_patch_skipping(root, "8 9")
    assert result.returncode == 0, result.stderr
    return result


def _stub_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "stubs"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        path = bin_dir / name
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _dv_root(tmp_path: Path) -> Path:
    """A DV_ROOT whose generic test environment is the repository's."""
    dv = tmp_path / "dv"
    if dv.exists():
        return dv
    include = dv / "verif" / "diag" / "assembly" / "include" / "riscv" / "generic"
    shutil.copytree(GENERIC_INCLUDE, include)
    (include / "link.ld").write_text(LINK_LD)
    return dv


def _run_rv32_as(tmp_path: Path, root: Path, program: str, *, path: str, extra_env: dict | None = None,
                 tag: str = "run"):
    """Run the patched rv32_as in a fresh run directory holding *program*."""
    run_dir = tmp_path / tag
    run_dir.mkdir()
    (run_dir / program).write_text("int main(void) { return 0; }\n")
    env = {
        **os.environ,
        "PATH": path,
        "DV_ROOT": str(_dv_root(tmp_path)),
        "RV32_ENV": "generic",
        "RV32_TARGET_TRIPLE": "riscv64-unknown-elf",
        "RV32_MARCH": "rv32ima",
        "RV32_MABI": "ilp32",
        "ASM_DIAG_NAME_NO_EXT": "prog",
        "STUB_LOG": str(tmp_path / "stub.log"),
        **(extra_env or {}),
    }
    if not extra_env or "RV32_IMAGE_BASE" not in extra_env:
        env.pop("RV32_IMAGE_BASE", None)
    result = subprocess.run(
        ["bash", str(root / RV32_AS)], cwd=run_dir, env=env, capture_output=True, text=True, timeout=60,
    )
    return run_dir, result


class TestFix19Text:
    def test_adds_the_c_branch_and_the_image_base(self, rv32_as_root):
        _patched(rv32_as_root)
        text = (rv32_as_root / RV32_AS).read_text()
        assert "if [ -f diag.c ]; then" in text
        assert "$INC/crt.S $INC/syscalls.c diag.c -lgcc -o diag.exe || exit 1" in text
        assert "-nostdlib -nostartfiles -fno-common -fno-builtin-printf -msmall-data-limit=0" in text
        assert 'printf "\\n@${RV32_IMAGE_BASE:-0000000040000000}\\t' in text
        assert "@0000000040000000" not in text.replace("${RV32_IMAGE_BASE:-0000000040000000}", "")

    def test_the_assembly_command_stays(self, rv32_as_root):
        _patched(rv32_as_root)
        text = (rv32_as_root / RV32_AS).read_text()
        assert "-DTEST_FUNC_NAME=${ASM_DIAG_NAME_NO_EXT}" in text
        assert text.count("diag.S -o diag.exe\nfi\n") == 1

    def test_is_idempotent(self, rv32_as_root):
        _patched(rv32_as_root)
        after_first = (rv32_as_root / RV32_AS).read_text()
        second = _patched(rv32_as_root)
        assert "(fix 19)" in second.stdout
        assert (rv32_as_root / RV32_AS).read_text() == after_first

    def test_patch_skip_leaves_rv32_as_alone(self, rv32_as_root):
        result = _run_patch_skipping(rv32_as_root, "8 9 19")
        assert result.returncode == 0, result.stderr
        assert "skipped fix 19 (PATCH_SKIP)" in result.stdout
        text = (rv32_as_root / RV32_AS).read_text()
        assert "RV32_IMAGE_BASE" not in text and "diag.c" not in text

    def test_a_changed_anchor_fails_loudly(self, rv32_as_root):
        path = rv32_as_root / RV32_AS
        path.write_text(path.read_text().replace("-nostartfiles   \\\n", "-nostartfiles \\\n"))
        result = _run_patch_skipping(rv32_as_root, "8 9")
        assert result.returncode != 0
        assert "rv32_as: expected exactly 1 match" in result.stderr

    def test_a_script_without_the_image_base_line_is_skipped(self, generic_piton_root):  # noqa: F811  (a fixture, used by name)
        result = _run_patch_skipping(generic_piton_root, "8 9")
        assert result.returncode == 0, result.stderr
        assert "not found, skipping fix 19" in result.stdout
        assert "RV32_IMAGE_BASE" not in (generic_piton_root / RV32_AS).read_text()


class TestPatchedRv32AsWithStubs:
    def test_a_c_program_gets_the_runtime(self, rv32_as_root, tmp_path):
        _patched(rv32_as_root)
        run_dir, result = _run_rv32_as(tmp_path, rv32_as_root, "diag.c", path=f"{_stub_bin(tmp_path)}:{os.environ['PATH']}")
        assert result.returncode == 0, result.stderr
        gcc_calls = (tmp_path / "stub.log").read_text().splitlines()
        build = [line for line in gcc_calls if "diag.exe" in line]
        assert len(build) == 1
        for part in ("-march=rv32ima", "-mabi=ilp32", "-nostdlib", "-nostartfiles", "/generic/crt.S",
                     "/generic/syscalls.c", "diag.c", "-lgcc", "-T", "/generic/link.ld"):
            assert part in build[0], part
        assert "-DTEST_FUNC_NAME" not in build[0]
        assert (run_dir / "symbol.tbl").read_text() == "good_trap 00000010 X 00000010\nbad_trap 00000020 X 00000020\n"

    def test_picolibc_is_used_only_when_its_specs_exist(self, rv32_as_root, tmp_path):
        _patched(rv32_as_root)
        path = f"{_stub_bin(tmp_path)}:{os.environ['PATH']}"
        _run_rv32_as(tmp_path, rv32_as_root, "diag.c", path=path, tag="without")
        assert "--specs=picolibc.specs" not in (tmp_path / "stub.log").read_text()
        (tmp_path / "stub.log").unlink()
        _run_rv32_as(tmp_path, rv32_as_root, "diag.c", path=path, tag="with",
                     extra_env={"STUB_SPECS": "/usr/lib/picolibc/picolibc.specs"})
        assert "--specs=picolibc.specs" in (tmp_path / "stub.log").read_text()

    def test_an_assembly_program_keeps_todays_command(self, rv32_as_root, tmp_path):
        _patched(rv32_as_root)
        _, result = _run_rv32_as(tmp_path, rv32_as_root, "diag.S", path=f"{_stub_bin(tmp_path)}:{os.environ['PATH']}")
        assert result.returncode == 0, result.stderr
        build = [line for line in (tmp_path / "stub.log").read_text().splitlines() if "diag.exe" in line]
        assert len(build) == 1
        assert "diag.S -o diag.exe" in build[0] and "-DTEST_FUNC_NAME=prog" in build[0]
        assert "crt.S" not in build[0] and "syscalls.c" not in build[0]

    def test_the_image_starts_at_the_default_base(self, rv32_as_root, tmp_path):
        _patched(rv32_as_root)
        run_dir, _ = _run_rv32_as(tmp_path, rv32_as_root, "diag.c", path=f"{_stub_bin(tmp_path)}:{os.environ['PATH']}")
        assert (run_dir / "mem.image").read_text().splitlines()[1].startswith("@0000000040000000\t")

    def test_the_image_starts_at_rv32_image_base(self, rv32_as_root, tmp_path):
        _patched(rv32_as_root)
        run_dir, _ = _run_rv32_as(
            tmp_path, rv32_as_root, "diag.S", path=f"{_stub_bin(tmp_path)}:{os.environ['PATH']}",
            extra_env={"RV32_IMAGE_BASE": "0000000080000000"},
        )
        assert (run_dir / "mem.image").read_text().splitlines()[1].startswith("@0000000080000000\t")

    def test_a_test_environment_without_a_runtime_stops_the_build(self, rv32_as_root, tmp_path):
        _patched(rv32_as_root)
        dv = _dv_root(tmp_path)
        (dv / "verif/diag/assembly/include/riscv/generic/crt.S").unlink()
        _, result = _run_rv32_as(tmp_path, rv32_as_root, "diag.c", path=f"{_stub_bin(tmp_path)}:{os.environ['PATH']}")
        assert result.returncode == 1
        assert "has no crt.S" in result.stderr


C_PROGRAMS = ("generic_c_hello.c", "generic_c_flags.c", "generic_c_fail.c")
# The gate workloads that need the A extension: they use amo instructions.
ATOMIC_WORKLOADS = ("barrier_atomic.c", "producer_consumer.c", "scatter_gather.c", "matmul_scaled.c")


@pytest.mark.skipif(shutil.which("riscv64-unknown-elf-gcc") is None, reason="needs riscv64-unknown-elf-gcc")
class TestRealCompiler:
    """Every program the ladder runs builds against the runtime, and the
    workloads under mace/workloads/ build as they are."""

    @pytest.mark.parametrize("march", ["rv32i", "rv32im", "rv32ima", "rv32imc"])
    def test_programs_build_with_pass_and_fail_symbols(self, rv32_as_root, tmp_path, march):
        _patched(rv32_as_root)
        dv = _dv_root(tmp_path)
        programs = [GENERIC_PROGRAMS / name for name in C_PROGRAMS]
        for source in sorted(WORKLOADS.glob("*.c")):
            if source.name in ATOMIC_WORKLOADS and "a" not in march[4:]:
                continue
            programs.append(source)
        assert len(programs) >= 4
        for source in programs:
            run_dir = tmp_path / f"{march}_{source.stem}"
            run_dir.mkdir()
            shutil.copy(source, run_dir / "diag.c")
            result = subprocess.run(
                ["bash", str(rv32_as_root / RV32_AS)], cwd=run_dir, capture_output=True, text=True, timeout=120,
                env={**os.environ, "DV_ROOT": str(dv), "RV32_ENV": "generic", "RV32_MARCH": march,
                     "RV32_MABI": "ilp32", "RV32_TARGET_TRIPLE": "riscv64-unknown-elf",
                     "ASM_DIAG_NAME_NO_EXT": source.stem},
            )
            assert result.returncode == 0, f"{march} {source.name}: {result.stderr[-800:]}"
            assert (run_dir / "diag.exe").is_file(), f"{march} {source.name}: {result.stdout[-800:]}"
            symbols = (run_dir / "symbol.tbl").read_text().split()
            assert symbols.count("good_trap") == 1 and symbols.count("bad_trap") == 1, f"{march} {source.name}"
            assert (run_dir / "mem.image").read_text().splitlines()[1].startswith("@0000000040000000\t")

    def test_an_atomic_workload_needs_the_a_extension(self, rv32_as_root, tmp_path):
        _patched(rv32_as_root)
        run_dir = tmp_path / "noa"
        run_dir.mkdir()
        shutil.copy(WORKLOADS / "barrier_atomic.c", run_dir / "diag.c")
        result = subprocess.run(
            ["bash", str(rv32_as_root / RV32_AS)], cwd=run_dir, capture_output=True, text=True, timeout=120,
            env={**os.environ, "DV_ROOT": str(_dv_root(tmp_path)), "RV32_ENV": "generic", "RV32_MARCH": "rv32im",
                 "RV32_MABI": "ilp32", "RV32_TARGET_TRIPLE": "riscv64-unknown-elf",
                 "ASM_DIAG_NAME_NO_EXT": "barrier_atomic"},
        )
        assert result.returncode != 0
