"""Real, one-shot proof: a real LLM backend, given TestbenchEditTool, can
reconcile a create_env.py-scaffolded unit-test testbench against a real
module's ports well enough for the build to actually succeed.

Not a permanent addition -- a diagnostic script, matching this project's own
scripts/local_*_test.py convention. Targets alarm_counter (piton/design/
common/rtl/alarm_counter.v): a small, self-contained, real module (4 ports,
no includes, no macros) chosen specifically to keep one real LLM call cheap
while still proving the mechanism end to end.

Uses Vertex/Gemini on GCP -- this project's only funded LLM credits (see
memory: opencode and other backends have no available credits). Needs GCP
Application Default Credentials (`gcloud auth application-default login`);
the project/model below were confirmed reachable directly against the
Vertex REST API before this script was written, not guessed.

Run (from the MACE repo root, chia_env active, WSL):
    python scripts/local_unit_test_edit_llm_test.py [piton_root]
"""

from __future__ import annotations

import sys

import ray

from chia.models.vertex import VertexGeminiLLM
from mace.loop import run_mace_step
from mace.spec import MaceSpec, Task

PITON_ROOT = sys.argv[1] if len(sys.argv) > 1 else "/mnt/c/Users/Potato/Desktop/openpiton"
GCP_PROJECT = "mace-508004"
GEMINI_MODEL = "gemini-2.5-flash"


def main() -> None:
    ray.init(address="local", resources={"openpiton": 1, "vertex_creds": 1}, log_to_driver=False)
    try:
        llm = VertexGeminiLLM(model=GEMINI_MODEL, project=GCP_PROJECT, location="us-central1")
        spec = MaceSpec(
            workloads=("hello_world.c",),
            objective="unit test alarm_counter",
        )
        task = Task(
            id="t1", deps=(), kind="unit_test", spec="piton/design/common/rtl/alarm_counter.v"
        )

        print(f"piton_root={PITON_ROOT}")
        print("Dispatching real LLM call via run_mace_step (kind=unit_test)...")
        result = run_mace_step(PITON_ROOT, spec, task, llm)

        print("\n=== LLM response ===")
        print(result.query.result)
        print("\n=== Build result ===")
        print(f"success={result.build.success}")
        print(f"returncode={result.build.returncode}")
        print(f"failure_reason={result.build.failure_reason!r}")
        if not result.build.success:
            print("\n=== Build stderr (tail) ===")
            print(result.build.stderr[-4000:])
        print(f"\npassed={result.passed}")
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()
