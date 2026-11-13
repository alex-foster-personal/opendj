"""SYNC-12965: ratings outside the integer 0..5 domain fail before any vendor write.

[if] a ratings diff or djay patch carries a rating outside 0..5 [then] it raises ValueError before either DB is touched, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync import apply_ratings
from apps.sync.djay_writer import patch_rating

pytestmark = pytest.mark.requirement("SYNC-12965")

_BLOB = b"TSAF" + b"\x00" * 12 + b"\x0b\x05\x00\x00"


def _rows(rb_value: str, djay_value: str) -> list[dict[str, str]]:
    return [
        {
            "rb_content_id": "10",
            "djay_uuid": "u-1",
            "field": "rating",
            "rb_value": rb_value,
            "djay_value": djay_value,
            "resolution": "accept_rb",
        },
        {
            "rb_content_id": "11",
            "djay_uuid": "u-2",
            "field": "rating",
            "rb_value": "3",
            "djay_value": "3",
            "resolution": "accept_djay",
        },
    ]


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record vendor-apply calls instead of touching any database."""
    calls: list[str] = []

    def _rb(targets: list[tuple[str, int]], **_kw: object) -> int:
        calls.append("rb")
        return len(targets)

    def _djay(targets: list[tuple[str, int]], **_kw: object) -> int:
        calls.append("djay")
        return len(targets)

    monkeypatch.setattr(apply_ratings, "_apply_rb_rating_targets", _rb)
    monkeypatch.setattr(apply_ratings, "_apply_djay_rating_targets", _djay)
    return calls


@pytest.mark.parametrize(("rb_value", "djay_value"), [("-1", "3"), ("6", "3"), ("3", "-2"), ("3", "9")])
def test_live_run_rejects_out_of_domain_rating_before_any_write(
    rb_value: str, djay_value: str, writes: list[str], tmp_path: Path
) -> None:
    """[if] a diff row holds a negative or >5 rating [then] live_run raises before either vendor write, [else stop]."""
    with pytest.raises(ValueError, match=r"0\.\.5 rating domain"):
        apply_ratings.live_run(
            _rows(rb_value, djay_value),
            rb_db_path=tmp_path / "rb.db",
            djay_db_path=tmp_path / "djay.db",
            flag_ok=True,
        )
    assert writes == []


@pytest.mark.parametrize("value", ["0", "1", "2", "3", "4", "5"])
def test_live_run_accepts_in_domain_ratings(value: str, writes: list[str], tmp_path: Path) -> None:
    """[if] every diff rating is an integer 0..5 [then] both vendor writes are planned, [else stop]."""
    rc = apply_ratings.live_run(
        _rows(value, value),
        rb_db_path=tmp_path / "rb.db",
        djay_db_path=tmp_path / "djay.db",
        flag_ok=True,
    )
    assert rc == 0
    assert writes == ["rb", "djay"]


@pytest.mark.parametrize("value", [-1, 6, 255])
def test_patch_rating_rejects_out_of_domain_instead_of_clamping(value: int) -> None:
    """[if] patch_rating gets a rating outside 0..5 [then] it raises ValueError, not a clamped write, [else stop]."""
    with pytest.raises(ValueError, match=r"0\.\.5 domain"):
        patch_rating(_BLOB, value)


@pytest.mark.parametrize("value", [0, 5])
def test_patch_rating_accepts_domain_bounds(value: int) -> None:
    """[if] patch_rating gets 0 or 5 [then] it returns a patched blob, [else stop]."""
    assert patch_rating(_BLOB, value) != _BLOB
