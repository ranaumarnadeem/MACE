"""One run that integrates a core into OpenPiton's generic tile.

The core's RTL is in the checkout and its core descriptor names a wrapper
that does not exist yet: the run writes a skeleton in its place, and the
method under test has to write the wrapper's body. MACE runs its loop with
RTL edits on; the retry agent runs with RTL designs (its retry_agent_rtl
method). Both get the same spec. --off turns one of the loop's parts off for
an ablation, and the run is recorded as method no_<part>.

Run (after examples/cores/serv/install.sh):
    conda activate chia_env
    python examples/generic_integrate.py --piton-root ~/openpiton \\
        --descriptor examples/cores/serv.yaml --method mace --mesh 1x1

Exits 0 when the run passes.
"""

from __future__ import annotations

import argparse
import dataclasses
import os

import ray

from chia_openpiton.core_descriptor import load_descriptor
from mace.baselines.retry_agent import run_retry_agent
from mace.llm import default_model_for_backend, make_llm
from mace.metrics import RunLabels, open_db, summary
from mace.orchestrator import run_mace_loop
from mace.spec import Budget, LoopOptions, MaceSpec

# The loop parts --off can turn off: LoopOptions fields that matter when the
# loop edits RTL.
OFF_PARTS = ("plan_check", "raw_evidence", "narrowing", "fast_path", "gate_pass_ends_run", "carry_edits")


def _parse_mesh(text: str) -> tuple[int, int]:
    x, sep, y = text.lower().partition("x")
    if not (sep and x.isdigit() and y.isdigit()):
        raise argparse.ArgumentTypeError(f"--mesh must look like 2x2, got {text!r}")
    return int(x), int(y)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--piton-root", required=True)
    ap.add_argument("--descriptor", required=True, help="The core descriptor's YAML file")
    ap.add_argument("--method", choices=("mace", "retry_agent_rtl"), default="mace")
    ap.add_argument("--off", choices=OFF_PARTS, default=None,
                    help="With --method mace: the loop part to turn off, for an ablation")
    ap.add_argument("--mesh", type=_parse_mesh, default=(1, 1))
    ap.add_argument("--workloads", nargs="+", default=None,
                    help="Gate programs; default addi.S and generic_tile_id.S")
    ap.add_argument("--backend", default="vertex")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-iterations", type=int, default=5)
    ap.add_argument("--max-usd", type=float, default=20.0)
    ap.add_argument("--max-wall-s", type=int, default=3 * 3600)
    ap.add_argument("--repeat", type=int, default=0, help="Recorded in the run's labels")
    ap.add_argument("--tag", default="", help="Appended to the run's task label, to tell batches apart")
    ap.add_argument("--db-path", default=os.path.abspath("runs/generic_integrate.db"))
    args = ap.parse_args()
    if args.off and args.method != "mace":
        ap.error("--off needs --method mace")
    if args.backend == "vertex" and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        ap.error("--backend vertex needs GOOGLE_CLOUD_PROJECT")

    desc = load_descriptor(args.descriptor)
    workloads = tuple(args.workloads or ("addi.S", "generic_tile_id.S"))
    x, y = args.mesh
    spec = MaceSpec(
        workloads=workloads,
        objective=(
            f"Integrate the {desc.core} core ({desc.top_module}) into OpenPiton's generic tile: write the "
            f"body of its wrapper, piton/design/chip/tile/{desc.wrapper}, so that {', '.join(workloads)} "
            f"pass on a {x}x{y} mesh."
        ),
        core="generic",
        core_descriptor=os.path.abspath(args.descriptor),
        target_mesh=args.mesh,
        budget=Budget(max_iterations=args.max_iterations, max_usd=args.max_usd, max_wall_s=args.max_wall_s),
    )
    root = os.path.abspath(args.piton_root)
    ray.init(address="local", resources={"openpiton": 1, f"{args.backend}_creds": 1}, include_dashboard=False)
    model = default_model_for_backend(args.model, args.backend)
    llm = make_llm(args.backend, **({"model": model} if model else {}))
    os.makedirs(os.path.dirname(args.db_path), exist_ok=True)
    db = open_db(args.db_path, ray_placement=False)
    method = f"no_{args.off}" if args.off else args.method
    labels = RunLabels(method=method, task=f"integrate-{desc.core}-{x}x{y}{args.tag}", repeat=args.repeat)
    if args.method == "mace":
        options = dataclasses.replace(LoopOptions(), rtl_edits=True, **({args.off: False} if args.off else {}))
        result = run_mace_loop((root,), spec, llm, db, labels=labels, options=options)
    else:
        result = run_retry_agent(root, spec, llm, db, labels=labels, rtl_edits=True)

    print(f"run_id={result.run_id} status={result.status}")
    for reason in result.unsupported:
        print(f"  unsupported: {reason}")
    for i, results in enumerate(result.iterations):
        for r in results:
            verdicts = ", ".join(f"{run.test}={run.verdict}" for run in r.runs) or "-"
            print(f"  iteration {i} {r.task.id} ({r.task.kind}): passed={r.passed} "
                  f"build={r.build.success} runs: {verdicts}")
            for problem in r.boot_problems:
                print(f"    boot: {problem}")
    for key, value in summary(db, result.run_id).items():
        print(f"  {key}: {value}")
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
