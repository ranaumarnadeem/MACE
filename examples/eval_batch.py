"""Run the evaluation: every method on every suite task, three times each.

Reads the task suite (examples/eval/tasks.yaml by default), expands it into
shuffled (task, method, repeat) jobs, and runs each job that has no
finished run in the database yet, so a stopped batch resumes where it left
off. Each job starts by emptying MACE's build directories on every
checkout. Every run lands in one database, labelled with its method, task,
and repeat; examples/eval_report.py turns that database into tables.

Methods (see mace/eval/runner.py): mace, one_shot, retry_agent, expert,
no_triage, one_checkout, build_check, and no_reuse run on bring-up tasks;
codesign_mace, codesign_random, codesign_grid, and codesign_bayes run on
co-design tasks. Each method runs only on the tasks it applies to.

Run:
    export GOOGLE_CLOUD_PROJECT=<your-gcp-project> MAKEFLAGS=-j1
    python examples/eval_batch.py --piton-root ~/openpiton --piton-root-2 ~/openpiton-b \\
        --methods mace,one_shot,retry_agent,expert --repeats 3
    python examples/eval_batch.py --piton-root ~/openpiton --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os

import ray

from mace.eval.runner import METHODS, RunEnv, environment_meta, finished_keys, plan_jobs, run_batch
from mace.eval.suite import load_suite
from mace.llm import default_model_for_backend, make_llm
from mace.metrics import DBReader, open_db

DEFAULT_SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval", "tasks.yaml")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--piton-root", required=True, help="First OpenPiton checkout")
    ap.add_argument("--piton-root-2", default=None, help="Second checkout, for the loop's parallel tasks")
    ap.add_argument("--suite", default=DEFAULT_SUITE)
    ap.add_argument(
        "--methods",
        default="mace,one_shot,retry_agent,expert,codesign_mace,codesign_random,codesign_grid,codesign_bayes",
        help=f"Comma-separated; any of {', '.join(METHODS)}",
    )
    ap.add_argument("--tasks", default=None, help="Comma-separated task ids; default every verified task")
    ap.add_argument("--include-unverified", action="store_true", help="Also run candidate tasks")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--expert-repeats", type=int, default=1)
    ap.add_argument("--shuffle-seed", type=int, default=0)
    ap.add_argument("--no-task-prompts", action="store_true",
                    help="Skip each task's own LLM call, whose reply changes nothing that is built")
    ap.add_argument("--keep-cache", action="store_true", help="Do not empty the build cache before each job")
    ap.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                    help="Recorded in every run's meta, e.g. --label reverted_fix=11 for a checkout "
                         "patched with PATCH_SKIP=11; repeatable")
    ap.add_argument("--backend", default="vertex")
    ap.add_argument("--model", default=None)
    ap.add_argument("--project", default=None, help="GCP project; defaults to GOOGLE_CLOUD_PROJECT")
    ap.add_argument("--db-path", default=os.path.abspath("runs/eval.db"))
    ap.add_argument("--dry-run", action="store_true", help="List the jobs and exit")
    args = ap.parse_args()

    tasks = load_suite(args.suite)
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
        missing = wanted - {t.id for t in tasks}
        if missing:
            ap.error(f"no such tasks in {args.suite}: {sorted(missing)}")
        tasks = tuple(t for t in tasks if t.id in wanted)
    elif not args.include_unverified:
        tasks = tuple(t for t in tasks if t.verified)
    methods = tuple(m.strip() for m in args.methods.split(",") if m.strip())
    labels = {}
    for item in args.label:
        key, sep, value = item.partition("=")
        if not sep or not key:
            ap.error(f"--label needs KEY=VALUE, got {item!r}")
        labels[key] = value
    jobs = plan_jobs(tasks, methods, args.repeats, once_repeats=args.expert_repeats, seed=args.shuffle_seed)

    if args.dry_run:
        # DBReader, not open_db: the first SQLiteNode call starts Ray.
        done = set()
        if os.path.isfile(args.db_path):
            reader = DBReader(args.db_path)
            try:
                done = finished_keys(reader)
            finally:
                reader.close()
        for job in jobs:
            mark = "done" if job.key in done else "todo"
            print(f"{mark}  {job.task.id:<28} {job.method:<13} repeat {job.repeat}")
        print(f"{sum(j.key not in done for j in jobs)} of {len(jobs)} jobs to run")
        return 0

    model = default_model_for_backend(args.model, args.backend)
    if args.project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = args.project
    if args.backend == "vertex" and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        ap.error("--backend vertex needs --project or GOOGLE_CLOUD_PROJECT")
    piton_roots = tuple(os.path.abspath(os.path.expanduser(p)) for p in (args.piton_root, args.piton_root_2) if p)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Same Ray setup as examples/mace_end_to_end.py: a fresh local instance,
    # one openpiton slot and one credential slot per checkout.
    ray.init(
        address="local",
        resources={"openpiton": len(piton_roots), f"{args.backend}_creds": len(piton_roots)},
        include_dashboard=False,
    )
    os.makedirs(os.path.dirname(args.db_path), exist_ok=True)
    db = open_db(args.db_path, ray_placement=False)
    llm = make_llm(args.backend, **({"model": model} if model else {}))
    env = RunEnv(
        piton_roots=piton_roots,
        llm=llm,
        db=db,
        meta={**environment_meta(piton_roots, args.backend, model), **labels},
        task_prompts=not args.no_task_prompts,
        clear_cache=not args.keep_cache,
    )

    def report(job, outcome):
        status = getattr(outcome, "status", f"error: {outcome}")
        run_id = getattr(outcome, "run_id", "-")
        print(f"{job.task.id:<28} {job.method:<13} repeat {job.repeat}  {status}  run_id={run_id}", flush=True)

    counts = run_batch(jobs, env, on_job=report)
    print(f"--- ran {counts['ran']}, skipped {counts['skipped']} already finished, "
          f"{counts['errors']} errors; db={args.db_path}")
    ray.shutdown()
    return 0 if counts["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
