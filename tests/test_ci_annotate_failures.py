from __future__ import annotations

from pathlib import Path

from scripts import ci_annotate_failures as annotate


def test_failure_lines_pick_failed_ids_and_summary() -> None:
    log = "....\nFAILED tests/test_x.py::test_a - AssertionError: boom\nERROR tests/test_y.py::test_b\n1 failed, 3 passed in 1.2s\n"
    assert annotate.failure_lines(log) == [
        "FAILED tests/test_x.py::test_a - AssertionError: boom",
        "ERROR tests/test_y.py::test_b",
        "1 failed, 3 passed in 1.2s",
    ]


def test_main_prints_annotations_and_handles_missing_log(tmp_path: Path, capsys) -> None:  # noqa: ANN001
    log = tmp_path / "pytest.log"
    log.write_text("FAILED tests/test_x.py::test_a - 100% wrong\n", encoding="utf-8")
    assert annotate.main(["x", str(log)]) == 0
    assert "::error title=pytest::FAILED tests/test_x.py::test_a - 100%25 wrong" in capsys.readouterr().out
    assert annotate.main(["x", str(tmp_path / "missing.log")]) == 0
    assert "no pytest.log" in capsys.readouterr().out
