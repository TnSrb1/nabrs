"""Tests of the CLI scripts/code_map.py: --out writes to the given path, --check does not
write and returns code 1 on a stale file (without writing), --help does not fall.
"""

from __future__ import annotations

from pathlib import Path

from scripts import code_map


def test_help_lists_check_and_out(capsys) -> None:
    try:
        code_map.main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "--out" in out
    assert "--check" in out


def test_out_writes_to_given_path(tmp_path: Path) -> None:
    target = tmp_path / "CODE_MAP.md"
    rc = code_map.main(["--out", str(target)])
    assert rc == 0
    assert target.exists()
    assert target.read_text(encoding="utf-8") == code_map.build()
    # does not touch the real docs/CODE_MAP.md
    assert code_map.OUT.exists()


def test_check_ok_on_fresh_file_then_stale_after_edit(tmp_path: Path) -> None:
    target = tmp_path / "CODE_MAP.md"
    assert code_map.main(["--out", str(target)]) == 0

    assert code_map.main(["--check", "--out", str(target)]) == 0

    before = target.read_text(encoding="utf-8")
    target.write_text(
        before + "\nмусор, которого не будет в build()\n", encoding="utf-8"
    )

    rc = code_map.main(["--check", "--out", str(target)])
    assert rc == 1
    # --check does not write: the file stayed broken, it was not overwritten with the fresh map
    assert target.read_text(encoding="utf-8") != code_map.build()


def test_check_missing_file_is_stale(tmp_path: Path) -> None:
    target = tmp_path / "does_not_exist.md"
    rc = code_map.main(["--check", "--out", str(target)])
    assert rc == 1
    assert not target.exists()
