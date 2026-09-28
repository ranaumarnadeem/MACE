"""Tier-0 tests for mace.eval.suite.

Run:
    pytest mace/test/test_eval_suite.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mace.eval.suite import SuiteError, load_suite

SUITE = Path(__file__).resolve().parents[2] / "examples" / "eval" / "tasks.yaml"


def write(tmp_path, text):
    path = tmp_path / "suite.yaml"
    path.write_text(text)
    return path


MINIMAL = """\
version: 1
defaults:
  budget: {max_iterations: 3, max_wall_s: 3600}
tasks:
  - id: pico-2x2-addi
    core: pico
    mesh: [2, 2]
    workloads: [addi.S]
    objective: Verify addi.S passes.
    verified: true
    expert:
      config_rtl: [CONFIG_DISABLE_BIST_CLEAR]
      caches: {l2: [131072, 4]}
"""


class TestTheRepositorySuite:
    def test_loads(self):
        tasks = load_suite(SUITE)
        assert len({t.id for t in tasks}) == len(tasks)

    def test_the_verified_tasks_are_the_table_1_cells(self):
        verified = {t.id for t in load_suite(SUITE) if t.verified}
        assert verified == {"ariane-2x2-barrier", "ariane-4x4-barrier", "pico-2x2-addi", "pico-4x4-addi"}

    def test_every_pico_task_names_the_bist_define(self):
        for task in load_suite(SUITE):
            if task.core == "pico":
                assert "CONFIG_DISABLE_BIST_CLEAR" in task.objective
                assert task.expert_config_rtl == ("CONFIG_DISABLE_BIST_CLEAR",)


class TestLoading:
    def test_a_task_becomes_a_spec_and_an_expert_task(self, tmp_path):
        (task,) = load_suite(write(tmp_path, MINIMAL))
        spec = task.spec()
        assert (spec.core, spec.target_mesh, spec.workloads) == ("pico", (2, 2), ("addi.S",))
        assert spec.budget.max_iterations == 3
        expert = task.expert_task()
        assert expert.config_rtl == ("CONFIG_DISABLE_BIST_CLEAR",)
        assert expert.caches_dict == {"l2": (131072, 4)}

    def test_a_task_budget_overrides_the_defaults_field_by_field(self, tmp_path):
        text = MINIMAL.replace("    verified: true\n", "    verified: true\n    budget: {max_wall_s: 7200}\n")
        (task,) = load_suite(write(tmp_path, text))
        assert (task.budget.max_iterations, task.budget.max_wall_s) == (3, 7200)

    @pytest.mark.parametrize(
        "old,new,message",
        [
            ("version: 1", "version: 2", "version"),
            ("id: pico-2x2-addi", "id: Pico 2x2", "task id"),
            ("mesh: [2, 2]", "mesh: [2]", "mesh"),
            ("workloads: [addi.S]", "workloads: []", "workloads"),
            ("core: pico", "core: rocket", "core"),
            ("l2: [131072, 4]", "l3: [131072, 4]", "expert cache"),
            ("l2: [131072, 4]", "l2: [0, 4]", "pico-2x2-addi"),
            ("CONFIG_DISABLE_BIST_CLEAR", "bad-define", "config_rtl"),
            ("verified: true", "verified: true\n    colour: red", "unknown keys"),
            ("max_iterations: 3", "max_iterations: 0", "defaults"),
        ],
    )
    def test_a_malformed_entry_fails_to_load(self, tmp_path, old, new, message):
        with pytest.raises(SuiteError, match=message):
            load_suite(write(tmp_path, MINIMAL.replace(old, new)))

    def test_duplicate_ids_fail(self, tmp_path):
        text = MINIMAL + MINIMAL.split("tasks:\n", 1)[1]
        with pytest.raises(SuiteError, match="duplicate"):
            load_suite(write(tmp_path, text))
