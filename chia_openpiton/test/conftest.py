"""Shared fixtures for the chia_openpiton tier-0 tests.

Run:
    conda activate chia_env && pytest chia_openpiton/test -q

Tier 0 needs no Ray, no cluster and no OpenPiton: a `@ChiaFunction` called
directly runs in the caller's process, and constructing the workspace node with
``require_colocated=False`` skips placement-group reservation. What the node
shells out to is a stub ``sims`` staged in a temporary checkout.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _disabled_profiler():
    """Keep CHIA's profiler singleton disabled for the whole session.

    A local `@ChiaFunction` call runs ``get_profiler()``, and the profiler's
    first construction calls ``ray.get_actor`` -- which makes Ray auto-init and
    try to join whatever cluster address happens to be lying around. Building
    the singleton while ``get_collector`` returns None pins it as disabled.
    (Copied from chia/database/test/test_sqlite_node_live.py.)
    """
    import chia.trace.profiler as profiler_mod

    original = profiler_mod.get_collector
    profiler_mod.get_collector = lambda namespace=None: None
    try:
        profiler_mod.reset_profiler()
        profiler_mod.get_profiler()
    finally:
        profiler_mod.get_collector = original
    yield
    profiler_mod.reset_profiler()


def fixture_text(name: str) -> str:
    """Read a captured log fixture by filename."""
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def fixtures():
    """Access captured log fixtures by name."""
    return fixture_text


# --- stub OpenPiton checkout -------------------------------------------------

# A stand-in for piton/tools/bin/sims. It records the exact argv it was called
# with (one invocation per line in $FAKE_SIMS_ARGV), then behaves per env:
#
#   FAKE_SIMS_VERDICT   pass|fail|timeout|maxcycles  -- which transcript to emit
#   FAKE_SIMS_FAIL_BUILD=1                           -- die like a failed build
#   FAKE_SIMS_SLEEP=<seconds>                        -- hang, to exercise timeouts
#   FAKE_SIMS_BIG_SIM_LOG=1                          -- write a real sim.log with
#                                                        the verdict line followed
#                                                        by >LOG_TAIL_BYTES of
#                                                        padding, reproducing a
#                                                        verbose multi-tile run
#                                                        where other tiles keep
#                                                        logging after the
#                                                        finishing tile's verdict
#
# The emitted strings are the real ones from OpenPiton's testbench monitors
# (pc_cmp.v.pyv / monitor.v.pyv), including their inconsistent spacing.
STUB_SIMS = r"""#!/bin/bash
printf '%s\n' "$*" >> "${FAKE_SIMS_ARGV:-/dev/null}"

build_id="rel-0.1"
sys="manycore"
for arg in "$@"; do
    case "$arg" in
        -build_id=*) build_id="${arg#-build_id=}" ;;
        -sys=*) sys="${arg#-sys=}" ;;
    esac
done
model_dir="$PITON_ROOT/build/$sys/$build_id"
# manycore's real Verilator binary is Vcmp_top; a non-manycore sys (a
# unit-test env) builds its own -toplevel=<name>-derived binary instead --
# V<sys>_top mirrors that shape closely enough to exercise the adapter's
# glob-based discovery (_find_model_binary) rather than its manycore fast
# path, without needing a real per-sys -toplevel= value in this stub.
if [ "$sys" = "manycore" ]; then
    model_binary="Vcmp_top"
else
    model_binary="V${sys}_top"
fi

if [ -n "$FAKE_SIMS_SLEEP" ]; then
    sleep "$FAKE_SIMS_SLEEP"
fi

# Model pyHP's real side effect: it writes generated .tmp.v files back into the
# source tree on every build, which is why two builds must not share a checkout.
mkdir -p "$PITON_ROOT/piton/verif/env/manycore"
echo "// generated for $build_id" > "$PITON_ROOT/piton/verif/env/manycore/pc_cmp.tmp.v"

case "$*" in
    *-vlt_build*|*-vcs_build*|*-msm_build*)
        echo "sims: creating model directory $model_dir"
        if [ "$FAKE_SIMS_FAIL_BUILD" = "1" ]; then
            echo "%Error: Exiting due to 1 error(s)"
            echo "sims: Caught a SIGDIE. failed building model at /x/sims,2.0 line 1572."
            exit 1
        fi
        mkdir -p "$model_dir/obj_dir"
        echo "#!/bin/true" > "$model_dir/obj_dir/$model_binary"
        chmod +x "$model_dir/obj_dir/$model_binary"
        exit 0
        ;;
    *_run*)
        case "${FAKE_SIMS_VERDICT:-pass}" in
            pass)      verdict_line="1234: Simulation -> PASS (HIT GOOD TRAP)" ;;
            fail)      verdict_line="1234 : Simulation -> FAIL(HIT BAD TRAP)" ;;
            timeout)   verdict_line="1234 : Simulation -> FAIL(TIMEOUT)" ;;
            maxcycles) verdict_line="1234 : Simulation -> (terminated by reaching max cycles = 1500000)" ;;
        esac
        if [ "$FAKE_SIMS_BIG_SIM_LOG" = "1" ]; then
            {
                echo "$verdict_line"
                for i in $(seq 1 2000); do echo "tile 3: activity after tile 0's own verdict, line $i"; done
            } > sim.log
        else
            echo "$verdict_line"
        fi
        exit 0
        ;;
esac
exit 0
"""

STUB_SETTINGS = r"""# stub piton_settings.bash
export DV_ROOT=$PITON_ROOT/piton
export MODEL_DIR=$PITON_ROOT/build
export PATH="$DV_ROOT/tools/bin:$PATH"
"""


@pytest.fixture
def stub_piton_root(tmp_path) -> Path:
    """A throwaway OpenPiton checkout: stub `sims`, stub settings, build dir."""
    root = tmp_path / "openpiton"
    tools_bin = root / "piton" / "tools" / "bin"
    tools_bin.mkdir(parents=True)
    (root / "build").mkdir()

    sims = tools_bin / "sims"
    sims.write_text(STUB_SIMS)
    sims.chmod(sims.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    (root / "piton" / "piton_settings.bash").write_text(STUB_SETTINGS)
    return root


@pytest.fixture
def sims_argv(tmp_path, monkeypatch) -> "ArgvLog":
    """Records every command line the stub `sims` was invoked with."""
    path = tmp_path / "sims_argv.txt"
    monkeypatch.setenv("FAKE_SIMS_ARGV", str(path))
    return ArgvLog(path)


class ArgvLog:
    """Reader for the stub sims' recorded invocations."""

    def __init__(self, path: Path):
        self.path = path

    def lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return [ln for ln in self.path.read_text().splitlines() if ln.strip()]

    def last(self) -> str:
        lines = self.lines()
        assert lines, "stub sims was never invoked"
        return lines[-1]

    def __len__(self) -> int:
        return len(self.lines())
