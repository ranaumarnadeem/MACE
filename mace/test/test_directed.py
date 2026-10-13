"""Tier-0 tests for mace.directed and the ``test`` task kind in mace.integrator.

Run:
    pytest mace/test/test_directed.py -q

No Ray and no OpenPiton: a fake node answers the build, run, collect, and
put_file calls, and the test-writing agent is replaced by a function that
leaves its tests in the sidecar the real tool would write.
"""

from __future__ import annotations

import json
import os

import pytest
from chia.base.llm_call import QueryResult

from chia_openpiton.state_def import PitonBuildArtifact, PitonCollectResult, PitonRunResult
from mace import directed
from mace.integrator import _run_batch, integrate_parallel, rtl_prompt
from mace.spec import DirectedTest, LoopOptions, MaceSpec, Task
from mace.test.conftest import FakeLLM
from mace.tools import TEST_SIDECAR
from mace.workloads import WORKLOADS_DIR

ROOT = "/root_a"
TARGET = "piton/design/chip/tile/pico/rtl/picorv32.v"


def make_spec(core="pico", **override):
    kwargs = {"workloads": ("sh.S",), "objective": "make sh.S pass", "core": core, "target_mesh": (2, 2)}
    kwargs.update(override)
    return MaceSpec(**kwargs)


def test_task(id="t", deps=(), spec="write a directed test"):
    return Task(id=id, deps=deps, kind="test", spec=spec)


def a_test(name="lh_sign", verdict="fail", evidence="", attempt=1, form="S"):
    return DirectedTest(name, TARGET, form, f"source of {name}\n", attempt=attempt, verdict=verdict, evidence=evidence)


class Ref:
    def __init__(self, value):
        self.value = value


def fake_get(ref):
    return ref.value


class Remote:
    """A ChiaFunction stand-in: ``node.<fn>.chia_remote(...)`` resolves through fake_get."""

    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    def chia_remote(self, *args, _chia_tag=None, **kwargs):
        self.calls.append((args, kwargs))
        return Ref(self.fn(*args, **kwargs))


class Node:
    """A workspace node: every program's verdict is looked up by name
    (default ``pass``; the string ``none`` gives a run with no verdict)."""

    def __init__(self, verdicts=None, build_ok=True, root=ROOT):
        self.piton_root = root
        self.task_options = {}
        self.verdicts = verdicts or {}
        self.sidecars: dict[str, list[dict]] = {}
        self.files: dict[str, str] = {}
        self.build = Remote(self._build)
        self.run = Remote(self._run)
        self.collect = Remote(self._collect)
        self.put_file = Remote(self._put_file)
        self.apply_edits = Remote(lambda edits, staged="": {"edits": dict(edits), "diff": "d"})
        self.revert_edits = Remote(lambda: [])
        self.build_ok = build_ok

    def _build(self, config, **kwargs):
        return PitonBuildArtifact(
            success=self.build_ok, returncode=0 if self.build_ok else 1, config=config, sim_type="vlt",
            model_dir="/x", binary_path="/x/V", wall_time_s=0.0, failure_reason="" if self.build_ok else "build_error",
        )

    def _run(self, config, program, asm_diag_root=None, **kwargs):
        verdict = self.verdicts.get(program, "pass")
        return PitonRunResult(
            success=verdict == "pass", returncode=0, test=program, sim_type="vlt", run_dir=f"/x/runs/{program}",
            verdict=None if verdict == "none" else verdict, wall_time_s=2.0, sim_log_tail=f"log of {program}",
            stdout="sims said DIE" if verdict == "none" else "",
        )

    def _collect(self, base_dir, patterns, max_bytes_per_file=None):
        files = {}
        for pattern in patterns:
            if pattern == TEST_SIDECAR and base_dir in self.sidecars:
                files[pattern] = json.dumps(self.sidecars[base_dir])
            elif f"{base_dir}/{pattern}" in self.files:
                files[pattern] = self.files[f"{base_dir}/{pattern}"]
        return PitonCollectResult(base_dir=base_dir, files=files)

    def _put_file(self, relpath, content):
        self.files[os.path.join(self.piton_root, relpath).replace(os.sep, "/")] = content
        return relpath


def reply(text="done"):
    return QueryResult(result=text, returncode=0, stderr="", stream_result="", success=True)


@pytest.fixture(autouse=True)
def fake_cluster(monkeypatch):
    monkeypatch.setattr("mace.integrator.get", fake_get)
    monkeypatch.setattr("mace.directed.get", fake_get)


def entries(*tests):
    return [{"name": t.name, "target": t.target, "form": t.form, "source": t.source} for t in tests]


def leaves(node, *written_by_attempt):
    """A test_session that leaves the n-th list of tests in the sidecar of attempt n."""
    seen = []

    def session(node_, spec, task, llm, tools, form, directory, tag, context, feedback):
        seen.append({"directory": directory, "tag": tag, "context": context, "feedback": feedback, "form": form})
        n = len(seen) - 1
        if n < len(written_by_attempt) and written_by_attempt[n]:
            node.sidecars[directory] = entries(*written_by_attempt[n])
        return reply(f"attempt {n + 1}")

    return session, seen


class TestPrompt:
    def test_an_assembly_prompt_shows_the_pico_skeleton(self):
        prompt = directed.test_prompt(make_spec(), test_task(), "S")
        assert "RVTEST_CODE_BEGIN" in prompt and "TEST_CASE(number, register" in prompt
        assert "argv[0][0]" not in prompt
        assert "Objective: make sh.S pass" in prompt and "Target mesh: 2x2 tiles (4 harts)" in prompt
        assert "they stay, and they must keep passing): sh.S" in prompt
        assert "Task t: write a directed test" in prompt

    def test_a_c_prompt_shows_the_ariane_skeleton(self):
        prompt = directed.test_prompt(make_spec("ariane"), test_task(), "c")
        assert "argv[0][0] is the hart id" in prompt and "RVTEST_CODE_BEGIN" not in prompt

    def test_the_context_and_the_feedback_appear_only_when_given(self):
        bare = directed.test_prompt(make_spec(), test_task(), "S")
        assert "earlier attempts" not in bare and "previous attempt" not in bare
        full = directed.test_prompt(make_spec(), test_task(), "S", context="gate sh.S failed", feedback="no verdict")
        assert "What the earlier attempts in this run reported:\ngate sh.S failed" in full
        assert "Your previous attempt gave no usable test:\nno verdict" in full

    def test_it_asks_for_checks_against_the_specification_and_a_target(self):
        prompt = " ".join(directed.test_prompt(make_spec(), test_task(), "S").split())
        assert "not against what the RTL currently does" in prompt
        assert "Set the target of each test to the RTL file" in prompt
        assert "up to 3 tests" in prompt


class TestCarrying:
    def test_a_later_test_of_the_same_name_replaces_an_earlier_one(self):
        old, new = a_test("x", "pass"), a_test("x", "fail")
        merged = directed.merge_tests(test_task("r", deps=("a", "b")), {"a": (old,), "b": (new,)})
        assert merged == (new,)

    def test_only_dependencies_are_inherited(self):
        assert directed.merge_tests(test_task("r", deps=("a",)), {"a": (a_test("x"),), "z": (a_test("y"),)}) == (a_test("x"),)

    def test_a_task_hands_on_its_inherited_tests_and_its_own_that_ran(self):
        inherited = a_test("old", "pass")
        own = (a_test("fresh", "fail"), a_test("broken", "no_verdict"), a_test("unbuilt", "not_built"))
        carried = directed.carry_tests(test_task("r", deps=("a",)), own, {"a": (inherited,)})
        assert [t.name for t in carried] == ["old", "fresh"]


class TestReport:
    def test_a_failing_test_shows_its_source_and_the_end_of_its_log(self):
        text = directed.tests_report((a_test("lh_sign", "fail", evidence="expected 0xffffaa00"),))
        assert "dt_lh_sign.S, about " + TARGET + ": fail on the design before your change" in text
        assert "source of lh_sign" in text and "expected 0xffffaa00" in text

    def test_a_passing_test_shows_no_log(self):
        assert "End of its log" not in directed.tests_report((a_test("ok", "pass", evidence="ignored"),))

    def test_the_rtl_prompt_lists_the_tests_and_says_they_can_be_wrong(self):
        task = Task(id="r", deps=("t",), kind="rtl", spec="fix it")
        prompt = " ".join(rtl_prompt(make_spec(), task, {}, tests=(a_test("lh_sign"),)).split())
        assert "every one of these tests must pass" in prompt and "can be wrong" in prompt
        assert "dt_lh_sign.S" in prompt
        assert "Directed tests" not in rtl_prompt(make_spec(), task, {})


class TestAttempts:
    def run(self, node, session, **kw):
        def attempt(n, directory, feedback):
            # run_test_attempts hands over (attempt, directory, feedback); the
            # integrator's closure fills in the rest, as this does.
            return session(node, make_spec(), test_task(), None, (), "S", directory, f"taga{n}", "", feedback)

        return directed.run_test_attempts(
            make_spec(), test_task(), "S", "tag", ROOT,
            session=attempt,
            read=lambda directory: directed.read_tests(node, directory),
            run_program=lambda program, directory: node._run(None, program, directory),
            explain=lambda run: directed.compile_log(node, run),
            **kw,
        )

    def test_a_test_that_ran_ends_the_task_whatever_its_verdict(self):
        node = Node({"dt_lh_sign.S": "fail"})
        session, seen = leaves(node, [a_test("lh_sign")])
        tests, runs, query = self.run(node, session)
        assert len(seen) == 1 and seen[0]["directory"] == f"{ROOT}/build/.mace_tests/taga1"
        (t,) = tests
        assert (t.name, t.attempt, t.verdict, t.wall_s) == ("lh_sign", 1, "fail", 2.0)
        assert t.evidence == "log of dt_lh_sign.S"
        assert [r.test for r in runs] == ["dt_lh_sign.S"] and query.result == "attempt 1"

    def test_a_passing_test_keeps_no_evidence(self):
        node = Node()
        session, _ = leaves(node, [a_test("ok")])
        (t,), _, _ = self.run(node, session)
        assert (t.verdict, t.evidence) == ("pass", "")

    def test_no_test_written_sends_the_agent_back(self):
        node = Node()
        session, seen = leaves(node, [], [a_test("late")])
        tests, runs, _ = self.run(node, session)
        assert len(seen) == 2 and "wrote no test" in seen[1]["feedback"]
        assert [t.attempt for t in tests] == [2]

    def test_a_test_with_no_verdict_comes_back_with_the_reason(self):
        node = Node({"dt_bad.S": "none"})
        node.files["/x/runs/dt_bad.S/rv32_as.log"] = "diag.S:12: Error: unknown opcode `lhx'"
        session, seen = leaves(node, [a_test("bad")], [a_test("good")])
        tests, runs, _ = self.run(node, session)
        assert "dt_bad.S gave no verdict" in seen[1]["feedback"] and "unknown opcode" in seen[1]["feedback"]
        assert [(t.name, t.attempt, t.verdict) for t in tests] == [("bad", 1, "no_verdict"), ("good", 2, "pass")]
        assert "unknown opcode" in tests[0].evidence

    def test_the_log_of_sims_stands_in_when_no_compile_log_exists(self):
        node = Node({"dt_bad.S": "none"})
        session, seen = leaves(node, [a_test("bad")], [a_test("good")])
        self.run(node, session)
        assert "sims said DIE" in seen[1]["feedback"]

    def test_a_hang_from_a_program_that_did_not_assemble_is_no_verdict(self):
        node = Node({"dt_bad.S": "timeout"})
        node.files["/x/runs/dt_bad.S/rv32_as.log"] = "ld: diag.o: undefined reference to `fail_1'"
        session, seen = leaves(node, [a_test("bad")], [a_test("good")])
        tests, _, _ = self.run(node, session, assembly_error=lambda run: directed.assembly_error(node, run))
        assert "dt_bad.S did not assemble" in seen[1]["feedback"] and "fail_1" in seen[1]["feedback"]
        assert [(t.name, t.attempt, t.verdict) for t in tests] == [("bad", 1, "no_verdict"), ("good", 2, "pass")]
        assert "fail_1" in tests[0].evidence

    def test_a_hang_of_a_program_that_assembled_is_a_verdict(self):
        node = Node({"dt_loop.S": "timeout"})
        node.files["/x/runs/dt_loop.S/rv32_as.log"] = "dt_loop\n"
        session, seen = leaves(node, [a_test("loop")])
        (t,), _, _ = self.run(node, session, assembly_error=lambda run: directed.assembly_error(node, run))
        assert len(seen) == 1 and (t.verdict, t.attempt) == ("timeout", 1)

    def test_one_test_that_ran_is_enough_even_if_a_sibling_did_not_compile(self):
        node = Node({"dt_bad.S": "none"})
        session, seen = leaves(node, [a_test("bad"), a_test("good")])
        tests, _, _ = self.run(node, session)
        assert len(seen) == 1
        assert sorted((t.name, t.verdict) for t in tests) == [("bad", "no_verdict"), ("good", "pass")]

    def test_three_attempts_at_most(self):
        node = Node({"dt_bad.S": "none"})
        session, seen = leaves(node, *[[a_test("bad")]] * 5)
        tests, runs, _ = self.run(node, session)
        assert len(seen) == directed.MAX_TEST_ATTEMPTS == 3
        assert [t.attempt for t in tests] == [1, 2, 3] and not directed.usable(tests)

    def test_no_attempt_starts_once_the_time_is_out(self):
        node = Node()
        session, seen = leaves(node, [a_test("x")])
        tests, runs, query = self.run(node, session, out_of_time=lambda: True)
        assert (tests, runs, query, seen) == ((), (), None, [])


class TestAssemblyError:
    run = PitonRunResult(
        success=False, returncode=0, test="dt_x.S", sim_type="vlt", run_dir="/x/runs/dt_x.S", verdict="timeout",
    )

    @pytest.mark.parametrize(
        "log",
        ["diag.S:12: Error: unknown opcode `lhx'", "diag.S:3: error: expected expression", "ld: undefined reference to `fail_2'"],
    )
    def test_the_assembler_and_linker_errors_are_found(self, log):
        node = Node()
        node.files["/x/runs/dt_x.S/rv32_as.log"] = "dt_x\n" + log
        assert log in directed.assembly_error(node, self.run)

    @pytest.mark.parametrize("log", [None, "dt_error_check\n", "diag.S:4: Warning: something harmless"])
    def test_a_clean_or_missing_log_is_no_error(self, log):
        node = Node()
        if log is not None:
            node.files["/x/runs/dt_x.S/rv32_as.log"] = log
        assert directed.assembly_error(node, self.run) == ""

    def test_a_run_with_no_directory_has_no_error(self):
        run = PitonRunResult(success=False, returncode=0, test="dt_x.S", sim_type="vlt", run_dir="", verdict="timeout")
        assert directed.assembly_error(Node(), run) == ""


class TestReadAndInstall:
    def test_the_sidecar_is_read_back_into_tests(self):
        node = Node()
        node.sidecars["/d"] = entries(a_test("one"), a_test("two"))
        tests = directed.read_tests(node, "/d")
        assert [(t.name, t.target, t.form, t.source) for t in tests] == [
            ("one", TARGET, "S", "source of one\n"), ("two", TARGET, "S", "source of two\n"),
        ]
        assert all(t.verdict == "no_verdict" for t in tests)

    @pytest.mark.parametrize("text", [None, "not json", '[{"name": "x"}]'])
    def test_a_missing_or_damaged_sidecar_means_no_tests(self, text):
        node = Node()
        if text is not None:
            node.files[f"/d/{TEST_SIDECAR}"] = text
        assert directed.read_tests(node, "/d") == ()

    def test_installed_tests_land_in_a_directory_of_their_own(self):
        node = Node()
        directory = directed.install_tests(node, "tagi", (a_test("one"), a_test("two", form="S")))
        assert directory == f"{ROOT}/build/.mace_tests/tagi"
        assert node.files[f"{directory}/dt_one.S"] == "source of one\n"
        assert node.files[f"{directory}/dt_two.S"] == "source of two\n"
        assert [args[0] for args, _ in node.put_file.calls] == [
            "build/.mace_tests/tagi/dt_one.S", "build/.mace_tests/tagi/dt_two.S",
        ]


class TestSession:
    def test_without_ray_the_agent_runs_with_no_tool_and_its_reply_comes_back(self):
        llm = FakeLLM(["wrote nothing"])
        query = directed.test_session(
            Node(), make_spec(), test_task(), llm, ("t",), "S", "/d", "tag", "ctx", "fb"
        )
        assert query.result == "wrote nothing"
        ((message, tools),) = llm.calls
        assert "What the earlier attempts in this run reported:\nctx" in message and "fb" in message
        assert list(tools) == ["t"]

    def test_a_failed_call_comes_back_as_a_failed_reply(self):
        query = directed.test_session(Node(), make_spec(), test_task(), FakeLLM([]), (), "S", "/d", "tag", "", "")
        assert query.returncode == 1 and query.stderr


class RateLimited(Exception):
    """What a backend raises on HTTP 429: it carries error_type "rate_limit"."""

    error_type = "rate_limit"


class Throwing:
    def __init__(self, error):
        self.error = error

    def prompt(self, message, tools=()):
        raise self.error


class FlakyThenFine:
    """Rate-limited for the first *times* calls, then answers."""

    def __init__(self, times):
        self.times = times
        self.calls = 0

    def prompt(self, message, tools=()):
        self.calls += 1
        if self.calls <= self.times:
            raise RateLimited("429")
        return reply("wrote the tests")


class TestRateLimit:
    """A rate limit says nothing about the tests or the repair. The agent calls wait and try again, and if the
    limit persists the run ends as an error, which the batch runs again."""

    @pytest.fixture(autouse=True)
    def no_waiting(self, monkeypatch):
        self.waits = []
        monkeypatch.setattr("mace.usage.time.sleep", lambda seconds: self.waits.append(seconds))

    def test_the_test_agent_waits_and_tries_again(self):
        llm = FlakyThenFine(times=2)
        query = directed.test_session(Node(), make_spec(), test_task(), llm, (), "S", "/d", "tag", "", "")
        assert query.result == "wrote the tests" and llm.calls == 3
        assert self.waits == [30, 60]

    def test_the_rtl_agent_waits_and_tries_again(self):
        from mace.integrator import _rtl_session

        task = Task(id="r", deps=(), kind="rtl", spec="fix it")
        llm = FlakyThenFine(times=1)
        query, _ = _rtl_session(Node(), make_spec(), task, llm, (), {}, "run1", 0)
        assert query.result == "wrote the tests" and self.waits == [30]

    def test_the_test_agent_lets_it_through_once_the_waits_are_used(self):
        llm = FlakyThenFine(times=99)
        with pytest.raises(RateLimited):
            directed.test_session(Node(), make_spec(), test_task(), llm, (), "S", "/d", "tag", "", "")
        assert llm.calls == 4 and self.waits == [30, 60, 120]

    def test_the_test_agent_lets_it_through(self):
        with pytest.raises(RateLimited):
            directed.test_session(
                Node(), make_spec(), test_task(), Throwing(RateLimited("429")), (), "S", "/d", "tag", "", ""
            )

    def test_the_test_agent_still_absorbs_any_other_error(self):
        query = directed.test_session(
            Node(), make_spec(), test_task(), Throwing(RuntimeError("boom")), (), "S", "/d", "tag", "", ""
        )
        assert query.returncode == 1 and "boom" in query.stderr

    def test_the_rtl_agent_lets_it_through(self):
        from mace.integrator import _rtl_session

        task = Task(id="r", deps=(), kind="rtl", spec="fix it")
        with pytest.raises(RateLimited):
            _rtl_session(Node(), make_spec(), task, Throwing(RateLimited("429")), (), {}, "run1", 0)

    def test_the_rtl_agent_still_absorbs_any_other_error(self):
        from mace.integrator import _rtl_session

        task = Task(id="r", deps=(), kind="rtl", spec="fix it")
        query, staged = _rtl_session(Node(), make_spec(), task, Throwing(RuntimeError("boom")), (), {}, "run1", 0)
        assert query.returncode == 1 and staged == ""

    def test_a_rate_limit_ends_the_run_as_an_error_through_the_batch(self, monkeypatch):
        def limited(node_, spec, task, llm, tools, form, directory, tag, context, feedback):
            raise RateLimited("429")

        monkeypatch.setattr("mace.directed.test_session", limited)
        with pytest.raises(RateLimited):
            batch(Node(), [test_task()])


def batch(node, tasks, applied_tests=None, options=None, spec=None, rtl_context="", llm=None):
    return _run_batch(
        [node] * len(tasks), spec or make_spec(), tasks, llm or FakeLLM([]), (), str(WORKLOADS_DIR), "run1", 0,
        options=options or LoopOptions(rtl_edits=True, task_prompts=False), rtl_context=rtl_context, applied_tests=applied_tests,
    )


class TestTestTask:
    def patch_session(self, monkeypatch, node, *written):
        session, seen = leaves(node, *written)
        monkeypatch.setattr("mace.directed.test_session", session)
        return seen

    def test_the_task_passes_when_its_test_ran_even_if_the_test_failed(self, monkeypatch):
        node = Node({"dt_lh_sign.S": "fail"})
        seen = self.patch_session(monkeypatch, node, [a_test("lh_sign")])
        (result,) = batch(node, [test_task()], rtl_context="gate sh.S failed")
        assert result.passed is True and result.build.success
        assert [(t.name, t.verdict) for t in result.tests] == [("lh_sign", "fail")]
        assert result.run.test == "dt_lh_sign.S" and result.run.verdict == "fail"
        assert seen[0]["context"] == "gate sh.S failed" and seen[0]["form"] == "S"
        # The test runs from its own directory, never from the gate workloads'.
        (args, kwargs), = node.run.calls
        assert args[1] == "dt_lh_sign.S" and kwargs["asm_diag_root"] == seen[0]["directory"]
        assert kwargs["asm_diag_root"] != str(WORKLOADS_DIR)

    def test_the_gate_workloads_are_not_run_by_the_test_task(self, monkeypatch):
        node = Node()
        self.patch_session(monkeypatch, node, [a_test("lh_sign")])
        batch(node, [test_task()])
        assert [args[1] for args, _ in node.run.calls] == ["dt_lh_sign.S"]

    def test_a_task_that_got_no_working_test_still_passes_with_its_attempts_on_record(self, monkeypatch):
        node = Node({"dt_bad.S": "none"})
        self.patch_session(monkeypatch, node, *[[a_test("bad")]] * 3)
        (result,) = batch(node, [test_task()])
        assert result.passed is True and [t.verdict for t in result.tests] == ["no_verdict"] * 3
        assert [t.attempt for t in result.tests] == [1, 2, 3]

    def test_a_task_whose_agent_wrote_nothing_still_passes(self, monkeypatch):
        node = Node()
        seen = self.patch_session(monkeypatch, node, [], [], [])
        (result,) = batch(node, [test_task()])
        assert result.passed is True and result.tests == () and result.run is None and len(seen) == 3

    def test_a_design_that_does_not_build_passes_without_calling_the_agent(self, monkeypatch):
        node = Node(build_ok=False)
        seen = self.patch_session(monkeypatch, node, [a_test("x")])
        (result,) = batch(node, [test_task()])
        assert result.passed is True and result.tests == () and result.run is None
        assert seen == [] and node.run.calls == []

    def test_a_build_only_check_writes_no_tests(self, monkeypatch):
        node = Node()
        seen = self.patch_session(monkeypatch, node, [a_test("x")])
        (result,) = batch(node, [test_task()], options=LoopOptions(check="build", task_prompts=False))
        assert result.passed is True and seen == [] and node.run.calls == []

    def test_a_core_with_no_test_form_fails_the_task(self, monkeypatch):
        node = Node()
        seen = self.patch_session(monkeypatch, node, [a_test("x")])
        (result,) = batch(node, [test_task()], spec=make_spec("sparc", workloads=("a.s",)))
        assert result.passed is False and "not available for sparc" in result.query.stderr and seen == []

    def test_the_rtl_edits_it_depends_on_are_applied_to_the_design_it_tests(self, monkeypatch):
        node = Node()
        self.patch_session(monkeypatch, node, [a_test("x")])
        edits = {"piton/design/a.v": "module a; endmodule\n"}
        (result,) = _run_batch(
            [node], make_spec(), [test_task("t2", deps=("r1",))], FakeLLM([]), (), str(WORKLOADS_DIR), "run1", 0,
            options=LoopOptions(rtl_edits=True, task_prompts=False), applied={"r1": edits},
        )
        assert node.apply_edits.calls[0][0] == (edits,) and result.edits == edits
        assert result.build.config.source_edits != "" and len(node.revert_edits.calls) == 1

    def test_the_agent_is_asked_in_the_task_s_own_directory_by_attempt(self, monkeypatch):
        node = Node({"dt_bad.S": "none"})
        seen = self.patch_session(monkeypatch, node, [a_test("bad")], [a_test("good")])
        batch(node, [test_task()])
        tag = directed.test_tag("run1", 0, "t")
        assert [s["directory"] for s in seen] == [
            f"{ROOT}/build/.mace_tests/{tag}a1", f"{ROOT}/build/.mace_tests/{tag}a2",
        ]
        assert [s["tag"] for s in seen] == [f"{tag}a1", f"{tag}a2"]


class TestInheritedTests:
    def rtl(self, deps=("t",)):
        return Task(id="r", deps=deps, kind="rtl", spec="fix the load")

    def staged_session(self, monkeypatch):
        seen = []

        def fake_session(node, spec, task, llm, tools, inherited, run_id, iteration, context="", **kwargs):
            seen.append(kwargs)
            return reply("changed"), ""

        monkeypatch.setattr("mace.integrator._rtl_session", fake_session)
        return seen

    def test_a_dependent_runs_the_tests_after_its_gate_workloads_from_their_own_directory(self, monkeypatch):
        node = Node()
        self.staged_session(monkeypatch)
        (result,) = batch(node, [self.rtl()], applied_tests={"t": (a_test("lh_sign", "fail"),)})
        assert result.passed is True
        assert [args[1] for args, _ in node.run.calls] == ["sh.S", "dt_lh_sign.S"]
        roots = [kwargs["asm_diag_root"] for _, kwargs in node.run.calls]
        tag = directed.test_tag("run1", 0, "r")
        assert roots == [str(WORKLOADS_DIR), f"{ROOT}/build/.mace_tests/{tag}i"]
        assert node.files[f"{ROOT}/build/.mace_tests/{tag}i/dt_lh_sign.S"] == "source of lh_sign\n"

    def test_the_task_fails_when_an_inherited_test_fails_on_the_changed_design(self, monkeypatch):
        node = Node({"dt_lh_sign.S": "fail"})
        self.staged_session(monkeypatch)
        (result,) = batch(node, [self.rtl()], applied_tests={"t": (a_test("lh_sign", "fail"),)})
        assert result.passed is False and result.run.test == "dt_lh_sign.S"
        assert [r.test for r in result.runs] == ["sh.S", "dt_lh_sign.S"]

    def test_a_failing_gate_workload_stops_before_the_tests(self, monkeypatch):
        node = Node({"sh.S": "fail"})
        self.staged_session(monkeypatch)
        (result,) = batch(node, [self.rtl()], applied_tests={"t": (a_test("lh_sign", "fail"),)})
        assert result.passed is False and [args[1] for args, _ in node.run.calls] == ["sh.S"]

    def test_the_rtl_agent_is_shown_the_tests(self, monkeypatch):
        node = Node()
        seen = self.staged_session(monkeypatch)
        batch(node, [self.rtl()], applied_tests={"t": (a_test("lh_sign", "fail"),)})
        assert [t.name for t in seen[0]["tests"]] == ["lh_sign"]

    def test_a_task_with_no_tests_calls_its_agent_as_before(self, monkeypatch):
        seen = self.staged_session(monkeypatch)
        (result,) = batch(Node(), [self.rtl(deps=())])
        assert seen == [{}] and result.passed is True

    def test_only_a_dependent_inherits_a_task_s_tests(self, monkeypatch):
        node = Node()
        self.staged_session(monkeypatch)
        batch(node, [self.rtl(deps=("other",))], applied_tests={"t": (a_test("lh_sign"),)})
        assert [args[1] for args, _ in node.run.calls] == ["sh.S"]


class TestAcrossLevels:
    def test_a_test_task_hands_its_tests_to_the_rtl_task_and_on_to_its_dependents(self, monkeypatch):
        node = Node()
        write, _ = leaves(node, [a_test("lh_sign")])
        monkeypatch.setattr("mace.directed.test_session", write)
        shown = []

        def fake_session(node_, spec, task, llm, tools, inherited, run_id, iteration, context="", **kwargs):
            shown.append([t.name for t in kwargs.get("tests", ())])
            return reply("changed"), ""

        monkeypatch.setattr("mace.integrator._rtl_session", fake_session)

        # The test fails where the test task ran it (the directory of its first
        # attempt) and passes where a dependent ran it after the repair.
        by_program = node._run

        def run(config, program, asm_diag_root=None, **kwargs):
            node.verdicts = {"dt_lh_sign.S": "fail"} if asm_diag_root.endswith("a1") else {}
            return by_program(config, program, asm_diag_root)

        node.run = Remote(run)
        tasks = (
            test_task("t"),
            Task(id="r", deps=("t",), kind="rtl", spec="fix the load"),
            Task(id="w", deps=("r",), kind="workload", spec="sh.S"),
        )
        results = integrate_parallel(
            (ROOT,), make_spec(), tasks, FakeLLM([]), nodes=[node], run_id="run1", options=LoopOptions(rtl_edits=True, task_prompts=False)
        )

        assert [(r.task.id, r.passed) for r in results] == [("t", True), ("r", True), ("w", True)]
        assert shown == [["lh_sign"]]
        # The test task ran its test once; the repair task and its dependent
        # each ran the gate workload, then the test.
        assert [args[1] for args, _ in node.run.calls] == [
            "dt_lh_sign.S", "sh.S", "dt_lh_sign.S", "sh.S", "dt_lh_sign.S",
        ]

    def test_a_test_task_that_got_no_working_test_does_not_block_the_repair(self, monkeypatch):
        node = Node({"dt_bad.S": "none"})
        write, _ = leaves(node, *[[a_test("bad")]] * 3)
        monkeypatch.setattr("mace.directed.test_session", write)
        shown = []

        def fake_session(node_, spec, task, llm, tools, inherited, run_id, iteration, context="", **kwargs):
            shown.append(kwargs)
            return reply("changed"), ""

        monkeypatch.setattr("mace.integrator._rtl_session", fake_session)
        tasks = (test_task("t"), Task(id="r", deps=("t",), kind="rtl", spec="fix it"))
        results = integrate_parallel(
            (ROOT,), make_spec(), tasks, FakeLLM([]), nodes=[node], run_id="run1",
            options=LoopOptions(rtl_edits=True, task_prompts=False),
        )
        assert [(r.task.id, r.passed) for r in results] == [("t", True), ("r", True)]
        # No test ran, so the repair task has none to show or to run.
        assert shown == [{}]
        assert [args[1] for args, _ in node.run.calls[-1:]] == ["sh.S"]

