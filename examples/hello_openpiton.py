"""Smallest end-to-end MACE loop: an agent edits RTL, CHIA rebuilds and checks it.

An agent is given a bash tool over an OpenPiton checkout and asked to add one
``$display`` to a testbench file. The freshly built model is then run, and we
grep its transcript for the marker -- so the agent's claim is never trusted; the
simulator's own output decides.

Fully local: no cloud, no Docker, no paid model. Run with:

    conda activate chia_env
    python examples/hello_openpiton.py --piton-root /path/to/openpiton

Dispatches are written as module-level ``@ChiaFunction``s assigned to plain
names (``ref = build_model.chia_remote(...)`` / ``value = get(ref)``) because
``chia viz`` reads the source statically: it only sees dispatch through a bare
name, so instance-member calls would render an empty graph.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess

import ray
from chia.base.ChiaFunction import ChiaFunction, get
from chia.base.tools.BashTool import BashTool
from chia.models.opencode import OpenCodeLLM

from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from chia_openpiton.state_def import PitonBuildArtifact, PitonConfig

MARKER = "MACE_HELLO_FROM_OPENPITON"

PROMPT = f"""Explore the OpenPiton verification environment under
piton/verif/env/manycore/. Add a SystemVerilog $display statement inside an
EXISTING `initial` block in one of the testbench/monitor files there (not the
synthesizable core RTL under piton/design/) that prints exactly:

  {MARKER}

Keep the change to a single added line, and don't touch anything else. After
editing, tell me which file and line you changed.
"""


@ChiaFunction(resources={"openpiton": 1})
def configure_model(piton_root: str, core: str) -> PitonConfig:
    """Resolve a 1x1 configuration against the checkout."""
    return OpenPitonWorkspaceNode.configure(piton_root, x_tiles=1, y_tiles=1, core=core)


@ChiaFunction(resources={"openpiton": 1})
def build_model(piton_root: str, config: PitonConfig) -> PitonBuildArtifact:
    """Build the Verilator model for this configuration."""
    return OpenPitonWorkspaceNode.build(piton_root, config, clean=True)


@ChiaFunction(resources={"openpiton": 1})
def model_startup_output(binary_path: str, timeout_seconds: int = 30) -> str:
    """Run the built model briefly and return its startup transcript.

    It exits complaining about a missing memory image -- no program is loaded
    here -- but ``initial`` blocks run at time 0, so a $display added to one
    prints before that point regardless.
    """
    proc = subprocess.Popen(
        [binary_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
    return out or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--piton-root", required=True, help="OpenPiton checkout on this machine")
    ap.add_argument("--core", default="sparc", choices=("sparc", "ariane"))
    ap.add_argument("--model", default="opencode/big-pickle")
    ap.add_argument("--skip-agent", action="store_true", help="build and check only")
    args = ap.parse_args()

    piton_root = os.path.abspath(args.piton_root)
    ray.init(resources={"openpiton": 1, "opencode_creds": 1})

    if not args.skip_agent:
        print("--- agent editing RTL ---")
        bash = BashTool(
            "openpiton_bash",
            piton_root,
            timeout_seconds=180,
            task_options={"resources": {"openpiton": 1}},
        )
        try:
            llm = OpenCodeLLM(model=args.model)
            reply = get(
                llm.prompt.options(resources={"opencode_creds": 1}).chia_remote(
                    llm, PROMPT, [bash]
                )
            )
            print(reply.result)
        finally:
            bash.stop()

    print("\n--- configure + build ---")
    # Dispatch and resolve in separate statements. `chia viz` reads this file
    # statically and pairs `ref = fn.chia_remote(...)` with `val = get(ref)`;
    # collapsing them into `val = get(fn.chia_remote(...))` renders an empty
    # graph.
    config_ref = configure_model.chia_remote(piton_root, args.core)
    config = get(config_ref)

    build_ref = build_model.chia_remote(piton_root, config)
    artifact = get(build_ref)
    print(
        f"build success={artifact.success} rc={artifact.returncode} "
        f"({artifact.wall_time_s:.0f}s) verilator={artifact.verilator_version}"
    )
    if not artifact.success:
        print(f"failure: {artifact.failure_reason}")
        print(artifact.stderr[-2000:])
        return 1

    print("\n--- run the built model ---")
    output_ref = model_startup_output.chia_remote(artifact.binary_path)
    output = get(output_ref)
    found = MARKER in output
    print(output[:800])
    print(f"\n{'PASS' if found else 'FAIL'}: marker {'found' if found else 'not found'}")
    return 0 if found else 1


if __name__ == "__main__":
    raise SystemExit(main())
