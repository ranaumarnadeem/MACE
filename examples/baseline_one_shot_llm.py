"""Baseline (b) from the plan doc's section 6.3: one-shot LLM config, no tools.

Same objective, same gate workload, same LLM backend as the proven MACE
end-to-end run (examples/mace_end_to_end.py) -- but a single prompt asking
directly for a configuration, applied once with no verification, no
iteration, and no failure-analysis/replan loop. This is the point of
comparison the paper needs: does MACE's iterate-until-verified loop
actually earn its cost over just asking an LLM once and trusting the
answer?

Deliberately does NOT import mace.loop/mace.orchestrator/mace.planner --
those modules *are* the thing being compared against. Uses mace.llm (for
the same backend-selection convention) and mace.spec/mace.workloads (so
the objective and gate workload are identical to the MACE run this
compares against), then drives chia_openpiton directly: configure, build,
run, record the verdict. One attempt. No retries, no triage, no replan --
if the LLM's answer doesn't already work, this fails, on purpose.

Run:
    conda activate chia_env
    python examples/baseline_one_shot_llm.py --piton-root /home/you/openpiton
"""
from __future__ import annotations

import argparse
import os
import re
import time

import ray

from chia.base.ChiaFunction import get
from chia_openpiton.openpiton_workspace import OpenPitonWorkspaceNode
from chia_openpiton.state_def import PitonConfig
from mace.llm import extract_cost_usd, make_llm
from mace.workloads import WORKLOADS_DIR

_PROMPT_TEMPLATE = """\
You are configuring OpenPiton/{core} to satisfy one objective, in a single
attempt -- there is no opportunity to see the result and try again, so
give your best answer directly.

Objective: {objective}
Gate workload (must pass): {workload}

Respond with exactly one line in this format (a footer, not prose):

CONFIG: x_tiles=<int> | y_tiles=<int> | l1i_size=<bytes> | l1i_assoc=<int> | \
l1d_size=<bytes> | l1d_assoc=<int> | l15_size=<bytes> | l15_assoc=<int> | \
l2_size=<bytes> | l2_assoc=<int>

Use the values you believe are most likely to work. Nothing else you write
is parsed, but keep the rest brief.
"""

_LINE_RE = re.compile(r"(?im)^\s*CONFIG:\s*(.+)$")


class OneShotConfigError(Exception):
    """The LLM's response produced no usable config line."""


def build_prompt(core: str, objective: str, workload: str) -> str:
    return _PROMPT_TEMPLATE.format(core=core, objective=objective, workload=workload)


def parse_config(text: str, core: str) -> PitonConfig:
    """The last well-formed CONFIG: line, or raise OneShotConfigError."""
    lines = _LINE_RE.findall(text)
    if not lines:
        raise OneShotConfigError(f"no CONFIG: line in the LLM's response: {text!r}")
    fields: dict[str, str] = {}
    for part in lines[-1].split("|"):
        if "=" not in part:
            continue
        key, _, value = part.strip().partition("=")
        fields[key.strip()] = value.strip()
    try:
        return PitonConfig(
            core=core,
            x_tiles=int(fields["x_tiles"]),
            y_tiles=int(fields["y_tiles"]),
            caches={
                "l1i": (int(fields["l1i_size"]), int(fields["l1i_assoc"])),
                "l1d": (int(fields["l1d_size"]), int(fields["l1d_assoc"])),
                "l15": (int(fields["l15_size"]), int(fields["l15_assoc"])),
                "l2": (int(fields["l2_size"]), int(fields["l2_assoc"])),
            },
        )
    except (KeyError, ValueError) as e:
        raise OneShotConfigError(f"CONFIG: line missing/malformed a field: {e}") from e


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--piton-root", required=True)
    ap.add_argument("--core", default="ariane", choices=("ariane", "sparc"))
    ap.add_argument("--backend", default="vertex", help="LLM backend -- vertex is this project's only funded one")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--project", default="mace-508004")
    ap.add_argument(
        "--objective",
        default="Verify the barrier_atomic gate workload passes on a 1x1 mesh.",
    )
    ap.add_argument("--workload", default="barrier_atomic.c")
    args = ap.parse_args()

    piton_root = os.path.abspath(args.piton_root)
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", args.project)
    # address="local": see examples/mace_end_to_end.py's ray.init() comment --
    # avoids silently attaching to a stale torn-down cluster's marker.
    ray.init(address="local", resources={"openpiton": 1, f"{args.backend}_creds": 1})

    llm = make_llm(args.backend, model=args.model)

    print("asking the LLM for a config, once, no tools...", flush=True)
    started = time.monotonic()
    prompt_query = llm.prompt(
        build_prompt(args.core, args.objective, args.workload), tools=[]
    )
    prompt_wall_s = time.monotonic() - started
    print(f"LLM response ({prompt_wall_s:.1f}s):\n{prompt_query.result}\n", flush=True)

    try:
        cfg_request = parse_config(prompt_query.result, args.core)
    except OneShotConfigError as e:
        print(f"BASELINE (one-shot): FAILED TO PARSE A CONFIG -- {e}", flush=True)
        ray.shutdown()
        return 1

    node = OpenPitonWorkspaceNode(piton_root, pg_ready_timeout_s=120)
    try:
        print(f"configuring: x_tiles={cfg_request.x_tiles} y_tiles={cfg_request.y_tiles} "
              f"caches={cfg_request.caches}", flush=True)
        cfg = get(node.configure.chia_remote(
            x_tiles=cfg_request.x_tiles, y_tiles=cfg_request.y_tiles,
            core=args.core, caches=cfg_request.caches,
        ))

        build_started = time.monotonic()
        art = get(node.build.chia_remote(cfg, timeout_seconds=3600))
        build_wall_s = time.monotonic() - build_started
        print(f"build wall_time={build_wall_s:.0f}s success={art.success}", flush=True)
        if not art.success:
            print(f"BASELINE (one-shot): FAIL at build -- {art.failure_reason}", flush=True)
            return 1

        run_started = time.monotonic()
        res = get(node.run.chia_remote(
            cfg, args.workload, asm_diag_root=str(WORKLOADS_DIR), timeout_seconds=1800
        ))
        run_wall_s = time.monotonic() - run_started
        total_wall_s = time.monotonic() - started
        cost_usd = extract_cost_usd(prompt_query)

        print(f"run wall_time={run_wall_s:.0f}s verdict={res.verdict} success={res.success}", flush=True)
        print(
            f"\n--- baseline (one-shot LLM, no tools) summary ---\n"
            f"  status: {'passed' if res.success else 'failed'}\n"
            f"  llm_calls: 1\n"
            f"  execution_time_s: {total_wall_s:.1f}\n"
            f"  compute_usd (lower bound): {cost_usd}\n"
            f"  verdict: {res.verdict}",
            flush=True,
        )
        return 0 if res.success else 1
    finally:
        node.close()
        ray.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
