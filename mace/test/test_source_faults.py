"""Tier-0 tests for mace.eval.source_faults and the runner's use of it.

Run:
    pytest mace/test/test_source_faults.py -q
"""

from __future__ import annotations

import pytest

from mace.eval.source_faults import FAULT_BACKUP_DIR, SourceEdit, apply, faulted, restore

PATH = "piton/design/core.v"
ORIGINAL = "module core;\n  assign a = b;\nendmodule\n"
FAULT = (SourceEdit(PATH, "assign a = b;", "assign a = bb;"),)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "piton" / "design").mkdir(parents=True)
    (tmp_path / PATH).write_text(ORIGINAL)
    return str(tmp_path)


def read(root):
    return (__import__("pathlib").Path(root) / PATH).read_text()


class TestSourceEdit:
    @pytest.mark.parametrize(
        "args",
        [("piton/verif/env/monitor.v", "a", "b"), ("piton/design/../verif/x.v", "a", "b"),
         ("/etc/passwd", "a", "b"), (PATH, "", "b"), (PATH, "a", "a")],
    )
    def test_bad_edits_are_refused(self, args):
        with pytest.raises(ValueError):
            SourceEdit(*args)


class TestApplyAndRestore:
    def test_apply_writes_the_fault_and_restore_returns_the_original(self, root):
        apply(root, FAULT)
        assert "assign a = bb;" in read(root)
        assert restore(root) == [PATH]
        assert read(root) == ORIGINAL

    def test_text_that_is_not_there_once_raises_and_leaves_the_file(self, root):
        with pytest.raises(ValueError, match="occurs 0 times"):
            apply(root, (SourceEdit(PATH, "assign x = y;", "assign x = z;"),))
        assert read(root) == ORIGINAL
        twice = (SourceEdit(PATH, "assign", "wire"),)
        (__import__("pathlib").Path(root) / PATH).write_text(ORIGINAL + ORIGINAL)
        with pytest.raises(ValueError, match="occurs 2 times"):
            apply(root, twice)
        assert read(root) == ORIGINAL + ORIGINAL

    def test_a_fault_a_crash_left_is_restored_before_the_next_apply(self, root):
        apply(root, FAULT)  # never restored
        apply(root, (SourceEdit(PATH, "assign a = b;", "assign a = c;"),))
        assert "assign a = c;" in read(root)
        restore(root)
        assert read(root) == ORIGINAL

    def test_restore_with_nothing_applied_is_a_no_op(self, root):
        assert restore(root) == []


class TestFaulted:
    def test_every_root_carries_the_fault_inside_the_block_and_none_after(self, root, tmp_path_factory):
        other = tmp_path_factory.mktemp("other")
        (other / "piton" / "design").mkdir(parents=True)
        (other / PATH).write_text(ORIGINAL)

        with faulted((root, str(other)), FAULT):
            assert "bb" in read(root) and "bb" in read(str(other))
        assert read(root) == ORIGINAL and read(str(other)) == ORIGINAL

    def test_the_files_are_restored_when_the_block_raises(self, root):
        with pytest.raises(RuntimeError):
            with faulted((root,), FAULT):
                raise RuntimeError("job died")
        assert read(root) == ORIGINAL

    def test_a_failure_on_a_later_root_restores_the_earlier_ones(self, root, tmp_path_factory):
        broken = tmp_path_factory.mktemp("broken")
        (broken / "piton" / "design").mkdir(parents=True)
        (broken / PATH).write_text("module core; endmodule\n")  # lacks the fault's old text

        with pytest.raises(ValueError):
            with faulted((root, str(broken)), FAULT):
                pass
        assert read(root) == ORIGINAL

    def test_the_backup_stays_out_of_the_rtl_edit_tools_backup(self):
        from chia_openpiton.openpiton_workspace import EDIT_BACKUP_DIR

        assert FAULT_BACKUP_DIR != EDIT_BACKUP_DIR
