"""has() / quota() / standing() with a subject and an EntitlementSource.

The seam billing plugs into. Two halves need defending: WITHOUT a source the
answers must be byte-identical to the shipped inert seam, and WITH one the
source must never be asked about nobody.

- [if] a configured source is consulted with subject=None [then] broken, [else stop].
- [if] a subject alone (no source) changes the inert answer [then] broken, [else stop].
- [if] a read_only or unknown subject reads as entitled [then] broken, [else stop].
"""

from __future__ import annotations

import pytest

from apps.entitlements import (
    PROVIDER_ENV,
    EntitlementProviderError,
    Standing,
    StaticEntitlementSource,
    has,
    quota,
    standing,
)

pytestmark = pytest.mark.requirement("CAT-04")

FEATURE: str = "zz-test.hosted-thing"
SUB: str = "google-sub-payer"


class RecordingSource:
    """A real :class:`StaticEntitlementSource`, plus a log of who it was asked about."""

    def __init__(self, table: dict[tuple[str, str], Standing]) -> None:
        self._inner = StaticEntitlementSource(table, provider="recording")
        self.asked: list[object] = []

    @property
    def provider(self) -> str:
        return self._inner.provider

    def standing(self, subject: str, feature_id: str) -> Standing | None:
        self.asked.append(subject)
        return self._inner.standing(subject, feature_id)


@pytest.fixture(autouse=True)
def _no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROVIDER_ENV, raising=False)


def _source(state: str, quota_value: int | None = 7) -> RecordingSource:
    return RecordingSource({(SUB, FEATURE): Standing(state=state, quota=quota_value)})  # type: ignore[arg-type]


def test_no_source_is_the_inert_answer_even_with_a_subject() -> None:
    """If passing a subject without a source changes the shipped answer then broken."""
    assert has(FEATURE) is True
    assert quota(FEATURE) is None
    assert has(FEATURE, subject=SUB) is True
    assert quota(FEATURE, subject=SUB) is None
    assert has(FEATURE, subject=None) is True


@pytest.mark.parametrize("subject", [None, "", "   "])
def test_a_source_is_never_consulted_without_a_subject(subject: str | None) -> None:
    """If a source is asked about subject=None then an unowned caller gets an answer."""
    source = _source("active")
    for call in (has, quota):
        with pytest.raises(ValueError, match="NOT consulted"):
            call(FEATURE, subject=subject, source=source)
    with pytest.raises(ValueError, match="NOT consulted"):
        standing(FEATURE, subject=subject, source=source)
    assert source.asked == []


def test_the_source_is_asked_about_exactly_the_subject_given() -> None:
    """If the resolver asks the source about anyone but the subject then broken."""
    source = _source("active")
    assert has(FEATURE, subject=SUB, source=source) is True
    assert source.asked == [SUB]


@pytest.mark.parametrize(
    ("state", "entitled"),
    [("active", True), ("past_due", True), ("read_only", False), ("archived", False)],
)
def test_has_follows_the_lifecycle(state: str, entitled: bool) -> None:
    """If read_only or archived reads as entitled, or past_due as not, then broken."""
    assert has(FEATURE, subject=SUB, source=_source(state)) is entitled


def test_quota_reports_the_source_value() -> None:
    """If a source's quota is replaced by unlimited then a cap nobody lifted vanishes."""
    assert quota(FEATURE, subject=SUB, source=_source("active", 7)) == 7
    assert quota(FEATURE, subject=SUB, source=_source("active", None)) is None


def test_a_subject_with_no_record_is_not_entitled() -> None:
    """If a never-subscribed subject reads as entitled then the hosted hub is free."""
    source = _source("active")
    other = "google-sub-never-paid"
    assert has(FEATURE, subject=other, source=source) is False
    assert quota(FEATURE, subject=other, source=source) == 0
    assert standing(FEATURE, subject=other, source=source) is None
    assert source.asked == [other, other, other]


def test_standing_returns_the_lifecycle_state() -> None:
    """If standing() loses the read_only vs archived distinction then pull cannot be kept."""
    found = standing(FEATURE, subject=SUB, source=_source("read_only"))
    assert found == Standing(state="read_only", quota=7)


def test_a_named_provider_still_fails_fast_before_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a source masks a misconfigured provider env then an operator believes billing is live."""
    monkeypatch.setenv(PROVIDER_ENV, "polar")
    source = _source("active")
    with pytest.raises(EntitlementProviderError):
        has(FEATURE, subject=SUB, source=source)
    assert source.asked == []


def test_standing_refuses_an_unknown_state_or_negative_quota() -> None:
    """If a source can report a state the lifecycle does not know then broken."""
    with pytest.raises(ValueError):
        Standing(state="cancelled", quota=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Standing(state="active", quota=-1)
