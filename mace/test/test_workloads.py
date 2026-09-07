"""Tier-0 tests for mace.workloads.

Run:
    pytest mace/test/test_workloads.py -q
"""

from __future__ import annotations

import hashlib

import pytest

from mace import workloads


def _write(tmp_path, name, content):
    (tmp_path / name).write_text(content)


class TestComputeChecksums:
    def test_matches_hashlib_directly(self, tmp_path):
        _write(tmp_path, "a.c", "int main() { return 0; }\n")
        got = workloads.compute_checksums(tmp_path)
        want = hashlib.sha256(b"int main() { return 0; }\n").hexdigest()
        assert got == {"a.c": want}

    def test_only_c_files_are_included(self, tmp_path):
        _write(tmp_path, "a.c", "x")
        _write(tmp_path, "CHECKSUMS", "irrelevant")
        _write(tmp_path, "notes.txt", "irrelevant")
        assert list(workloads.compute_checksums(tmp_path)) == ["a.c"]

    def test_empty_dir_is_empty(self, tmp_path):
        assert workloads.compute_checksums(tmp_path) == {}


class TestReadChecksums:
    def test_text_mode_two_space_separator(self, tmp_path):
        f = tmp_path / "CHECKSUMS"
        f.write_text("deadbeef  a.c\n")
        assert workloads.read_checksums(f) == {"a.c": "deadbeef"}

    def test_binary_mode_asterisk_separator(self, tmp_path):
        """The format sha256sum's own output uses (as produced on this repo)."""
        f = tmp_path / "CHECKSUMS"
        f.write_text("deadbeef *a.c\n")
        assert workloads.read_checksums(f) == {"a.c": "deadbeef"}

    def test_blank_lines_and_comments_are_skipped(self, tmp_path):
        f = tmp_path / "CHECKSUMS"
        f.write_text("# header comment\n\ndeadbeef  a.c\n\n")
        assert workloads.read_checksums(f) == {"a.c": "deadbeef"}

    def test_multiple_entries(self, tmp_path):
        f = tmp_path / "CHECKSUMS"
        f.write_text("aaaa  a.c\nbbbb  b.c\n")
        assert workloads.read_checksums(f) == {"a.c": "aaaa", "b.c": "bbbb"}


class TestVerifyChecksums:
    def _setup(self, tmp_path, content="int main() { return 0; }\n"):
        _write(tmp_path, "a.c", content)
        digest = hashlib.sha256(content.encode()).hexdigest()
        (tmp_path / "CHECKSUMS").write_text(f"{digest}  a.c\n")
        return tmp_path / "CHECKSUMS"

    def test_matching_files_pass_silently(self, tmp_path):
        checksums = self._setup(tmp_path)
        workloads.verify_checksums(tmp_path, checksums)  # must not raise

    def test_tampered_file_raises(self, tmp_path):
        checksums = self._setup(tmp_path)
        _write(tmp_path, "a.c", "int main() { return 1; }  // tampered\n")
        with pytest.raises(ValueError, match="checksum mismatch"):
            workloads.verify_checksums(tmp_path, checksums)

    def test_untracked_new_file_raises(self, tmp_path):
        checksums = self._setup(tmp_path)
        _write(tmp_path, "b.c", "int main() { return 0; }\n")
        with pytest.raises(ValueError, match="disagree"):
            workloads.verify_checksums(tmp_path, checksums)

    def test_missing_file_raises(self, tmp_path):
        checksums = self._setup(tmp_path)
        (tmp_path / "a.c").unlink()
        with pytest.raises(ValueError, match="disagree"):
            workloads.verify_checksums(tmp_path, checksums)


class TestRealWorkloads:
    """The committed mace/workloads/ directory itself, not a fixture."""

    def test_real_checksums_file_matches_real_workload_files(self):
        workloads.verify_checksums()  # must not raise

    def test_expected_gate_workloads_are_present(self):
        names = set(workloads.compute_checksums())
        assert names == {"barrier_atomic.c", "producer_consumer.c", "scatter_gather.c"}
