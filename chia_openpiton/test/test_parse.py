"""Tier-0 tests for chia_openpiton.parse.

Run:
    pytest chia_openpiton/test/test_parse.py -q

The build-failure and verilator fixtures are real captures from runs on this
machine. The simulation-transcript strings are taken verbatim from OpenPiton's
testbench source (pc_cmp.v.pyv / monitor.v.pyv) -- including the inconsistent
spacing before the colon -- pending real captures from the first Ariane run.
"""

from __future__ import annotations

import pytest

from chia_openpiton import parse

# Exactly as the RTL prints them. PASS has no space before the colon; FAIL does.
PASS_LINE = "1234500: Simulation -> PASS (HIT GOOD TRAP)"
BAD_TRAP_LINE = "1234500 : Simulation -> FAIL(HIT BAD TRAP)"
TIMEOUT_LINE = "1234500 : Simulation -> FAIL(TIMEOUT)"
MAXCYC_LINE = "1234500 : Simulation -> (terminated by reaching max cycles = 1500000)"


class TestSimVerdict:
    @pytest.mark.parametrize(
        "line,expected",
        [
            (PASS_LINE, "pass"),
            (BAD_TRAP_LINE, "fail"),
            (TIMEOUT_LINE, "timeout"),
            (MAXCYC_LINE, "maxcycles"),
        ],
    )
    def test_classifies_each_rtl_verdict(self, line, expected):
        assert parse.sim_verdict(f"noise\n{line}\nmore noise\n") == expected

    def test_no_verdict_returns_none(self):
        assert parse.sim_verdict("Info: spc(0) thread(0) -> starting\n") is None

    def test_empty_returns_none(self):
        assert parse.sim_verdict("") is None

    def test_fails_closed_when_pass_and_fail_both_present(self):
        """A verification gate must never be talked out of a failure."""
        assert parse.sim_verdict(f"{PASS_LINE}\n{BAD_TRAP_LINE}\n") == "fail"

    def test_timeout_is_not_reported_as_plain_fail(self):
        """Timeout is its own bucket: it means inconclusive, not proven wrong."""
        assert parse.sim_verdict(TIMEOUT_LINE) == "timeout"

    def test_monitor_message_is_recoverable(self):
        assert parse.fail_reason(BAD_TRAP_LINE) == "HIT BAD TRAP"

    def test_max_cycles_value(self):
        assert parse.max_cycles(MAXCYC_LINE) == 1500000
        assert parse.max_cycles(PASS_LINE) is None


class TestRealCaptures:
    """Against logs captured from a real Ariane 1x1 run on this machine."""

    def test_real_pass_transcript(self, fixtures):
        assert parse.sim_verdict(fixtures("run_pass_sim.log")) == "pass"

    def test_real_maxcycles_transcript(self, fixtures):
        assert parse.sim_verdict(fixtures("run_maxcycles_sim.log")) == "maxcycles"

    def test_sim_time_comes_off_the_verdict_line(self, fixtures):
        """These configs' status.log has no Cyc=, so this is the real source."""
        assert parse.sim_time(fixtures("run_pass_sim.log")) == 179911750

    def test_real_pass_status_log(self, fixtures):
        name, status = parse.status_diag(fixtures("run_pass_status.log"))
        assert name.startswith("hello_world.c")
        assert status == "PASS"

    def test_real_status_log_has_no_cycle_counts(self, fixtures):
        """Documents why sim_time exists: regreport emits no Cyc= here."""
        assert parse.cycles(fixtures("run_pass_status.log")) is None

    def test_unclassified_run_is_not_a_pass(self, fixtures):
        """A real -rtl_timeout expiry: regreport says 'Unknown (No Status)'."""
        _, status = parse.status_diag(fixtures("run_timeout_status.log"))
        assert status == "Unknown (No Status)"

    def test_timeout_happen_is_classified_when_no_verdict_line(self):
        """A real RTL timeout ends without ever printing FAIL(TIMEOUT)."""
        text = "Info: spc(0) thread(3) -> timeout happen\n"
        assert parse.sim_verdict(text) == "timeout"

    def test_timeout_happen_never_overrides_a_real_verdict(self):
        text = f"Info: spc(0) thread(0) -> timeout happen\n{PASS_LINE}\n"
        assert parse.sim_verdict(text) == "pass"


class TestStatusLog:
    # regreport renders "Diag: %-40s   %s"
    STATUS = (
        "Diag: ariane-hello-world:0                     PASS\n"
        "Mon Sep  8 03:14:00 PKT 2026\n"
        "Cyc=     185432, Sec=       12.4, C/S=14954.2\n"
        "ExecCyc=     180011, Sec=       12.4, EC/S=14517.0\n"
        "NumTiles=         1\n"
    )

    def test_diag_name_and_status(self):
        assert parse.status_diag(self.STATUS) == ("ariane-hello-world:0", "PASS")

    def test_cycle_counts(self):
        assert parse.cycles(self.STATUS) == 185432
        assert parse.exec_cycles(self.STATUS) == 180011

    def test_num_tiles(self):
        assert parse.num_tiles(self.STATUS) == 1

    def test_multiword_status_survives(self):
        """regreport's vocabulary includes multi-word statuses."""
        assert parse.status_diag("Diag: t:0     MaxCycles Hit\n")[1] == "MaxCycles Hit"

    def test_missing_fields_are_none(self):
        assert parse.cycles("nothing here") is None
        assert parse.status_diag("nothing here") is None


class TestRegressSummary:
    SUMMARY = (
        "\nSummary for /work/2026_09_08_1\n"
        + "=" * 80
        + "\n         Status:   ariane_tile1_simple |\n"
        + "-" * 80
        + "\n"
        "           PASS:        3 |\n"
        "           FAIL:        0 |\n"
        "        Timeout:        0 |\n"
        "  MaxCycles Hit:        0 |\n"
        + "-" * 80
        + "\n"
        "     Diag Count:        3 |\n"
        + "=" * 80
        + "\nREGRESSION PASSED\n"
        + "=" * 80
        + "\n"
    )

    def test_passed_verdict_and_counts(self):
        got = parse.regress_summary(self.SUMMARY)
        assert got["passed"] is True
        assert got["counts"]["PASS"] == 3
        assert got["counts"]["FAIL"] == 0
        assert got["diag_count"] == 3

    def test_failed_verdict(self):
        failed = self.SUMMARY.replace("REGRESSION PASSED", "REGRESSION FAILED")
        assert parse.regress_summary(failed)["passed"] is False

    def test_absent_verdict_is_none_not_false(self):
        """No verdict line means 'unknown', which must not read as 'failed'."""
        assert parse.regress_summary("partial output")["passed"] is None


class TestSimsOutput:
    def test_die_message_strips_perl_file_and_line(self, fixtures):
        text = fixtures("build_fail_bad_option.log")
        assert parse.sims_die(text) == "failed building model"

    def test_no_die_returns_empty(self):
        assert parse.sims_die("sims: version 2.0\n") == ""

    def test_model_dir_from_real_build_log(self, fixtures):
        got = parse.model_dir(fixtures("build_ok_tail.log"))
        assert got.endswith("/build/manycore/rel-0.1")


class TestBuildFailureReason:
    def test_needtiming_is_tagged(self, fixtures):
        """Verilator 5 against OpenPiton's #1-delay monitors."""
        assert (
            parse.build_failure_reason(fixtures("build_fail_needtiming.log"))
            == "verilator_needs_timing_flag"
        )

    def test_bad_option_is_tagged(self, fixtures):
        """A flag this Verilator does not have (e.g. --no-pch on 5.049)."""
        assert (
            parse.build_failure_reason(fixtures("build_fail_bad_option.log"))
            == "verilator_bad_option"
        )

    def test_pch_link_failure_is_tagged(self, fixtures):
        """verilated.mk's precompiled-header rule missing -c."""
        assert (
            parse.build_failure_reason(fixtures("build_fail_pch_link.log"))
            == "pch_link_failure"
        )

    def test_success_has_no_reason(self, fixtures):
        assert parse.build_failure_reason(fixtures("build_ok_tail.log")) == ""

    def test_stderr_is_searched_too(self):
        assert parse.build_failure_reason("", "%Error-NEEDTIMINGOPT: x") == (
            "verilator_needs_timing_flag"
        )


class TestVerilatorVersion:
    def test_release_string(self, fixtures):
        assert parse.verilator_version(fixtures("verilator_4038.txt")) == (4, 38)

    def test_devel_string(self, fixtures):
        assert parse.verilator_version(fixtures("verilator_5049.txt")) == (5, 49)

    def test_unparseable_is_none(self):
        assert parse.verilator_version("bash: verilator: command not found") is None

    def test_v5_needs_the_flag_and_v4_does_not(self, fixtures):
        """The bug this guards: v5 requires --no-timing, v4 rejects it."""
        assert parse.needs_no_timing(fixtures("verilator_5049.txt")) is True
        assert parse.needs_no_timing(fixtures("verilator_4038.txt")) is False

    def test_missing_verilator_does_not_add_the_flag(self):
        assert parse.needs_no_timing("") is False
