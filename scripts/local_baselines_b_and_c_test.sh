#!/bin/bash
# Runs baseline (b) (one-shot LLM, no tools) then baseline (c) (full MACE
# loop) back to back against the same real OpenPiton checkout, same
# objective/workload defaults in both scripts, for a clean side-by-side
# comparison. Sequential, not parallel, on purpose: both share one checkout,
# and pyHP's own build-time source-tree writes make two concurrent builds
# against the same checkout race each other.
#
# Real, used to produce the numbers in runs/baseline_b_output.log and
# runs/baseline_c_output.log this session -- kept as a reusable diagnostic,
# not a throwaway, matching this project's own scripts/local_*_test.py
# convention.
#
# Run:
#   bash scripts/local_baselines_b_and_c_test.sh [piton_root]
set -x
source ~/miniconda3/etc/profile.d/conda.sh 2>/dev/null || source ~/anaconda3/etc/profile.d/conda.sh 2>/dev/null
conda activate chia_env
cd "$(dirname "$0")/.."

PITON_ROOT="${1:-/mnt/c/Users/Potato/Desktop/openpiton}"

echo "=== BASELINE (b): one-shot LLM, no tools ===" > runs/baseline_b_output.log
python3 examples/baseline_one_shot_llm.py --piton-root "$PITON_ROOT" >> runs/baseline_b_output.log 2>&1
echo "BASELINE B EXIT CODE: $?" >> runs/baseline_b_output.log

echo "=== BASELINE (c): full MACE loop ===" > runs/baseline_c_output.log
python3 examples/mace_end_to_end.py --piton-root "$PITON_ROOT" >> runs/baseline_c_output.log 2>&1
echo "BASELINE C EXIT CODE: $?" >> runs/baseline_c_output.log

echo "ALL BASELINES DONE" >> runs/baseline_c_output.log
