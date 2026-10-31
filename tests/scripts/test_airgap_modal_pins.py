"""scripts/airgap_modal_pins.py: modal joins the airgap venv only at locked versions."""

from __future__ import annotations

from pathlib import Path

from scripts.airgap_modal_pins import constraints, drift, lock_pins, main, parse_freeze

LOCK = """
lock-version = "1.0"
[[packages]]
name = "modal"
version = "1.5.5"
[[packages]]
name = "grpclib"
version = "0.4.9"
[[packages]]
name = "Click"
version = "8.5.0"
"""
FREEZE = "-e file:///repo\nclick==8.4.1\nmusic_dj_tools @ file:///repo\nrequests==2.32.0\n"


def test_installed_versions_win_and_the_rest_come_from_the_lock() -> None:
    """[if] a package is installed [then] it keeps its version, else the CI lock's, [else stop]."""
    lines = constraints(parse_freeze(FREEZE), lock_pins(LOCK))
    assert lines == ["click==8.4.1", "grpclib==0.4.9", "modal==1.5.5", "requests==2.32.0"]


def test_verify_refuses_a_package_neither_lock_pins() -> None:
    """[if] the install added or moved a package off the CI lock [then] it is named, [else stop]."""
    lock = lock_pins(LOCK)
    before = parse_freeze(FREEZE)
    good = {**before, "modal": "1.5.5", "grpclib": "0.4.9"}
    assert drift(before, good, lock) == []
    bad = {**good, "synchronicity": "0.11.0", "requests": "2.33.0"}
    assert [line.split(" ")[0] for line in drift(before, bad, lock)] == ["requests==2.33.0", "synchronicity==0.11.0"]


def test_cli_round_trip(tmp_path: Path) -> None:
    lock = tmp_path / "pylock.toml"
    lock.write_text(LOCK, encoding="utf-8")
    before = tmp_path / "before.txt"
    before.write_text(FREEZE, encoding="utf-8")
    after = tmp_path / "after.txt"
    after.write_text(FREEZE + "modal==1.5.5\ngrpclib==0.4.9\n", encoding="utf-8")
    assert main(["verify", "--before", str(before), "--after", str(after), "--lock", str(lock)]) == 0
    after.write_text(FREEZE + "modal==1.6.0\n", encoding="utf-8")
    assert main(["verify", "--before", str(before), "--after", str(after), "--lock", str(lock)]) == 1
    empty = tmp_path / "empty.toml"
    empty.write_text('lock-version = "1.0"\n', encoding="utf-8")
    assert main(["constraints", "--installed", str(before), "--lock", str(empty)]) == 2
