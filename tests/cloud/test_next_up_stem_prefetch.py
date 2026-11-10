"""AutoPlay's next-up head gets its stem-set prefetched (STEM-11747).

[if] the head is cloud [then] it hydrates once with one prefetch in flight, [else stop].
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from apps.cloud.stem_hydration import (
    PREFETCH_FAILED_CODE,
    HydrationOutcome,
    NextUpStemPrefetcher,
    PrefetchDeps,
    prefetch_one,
)
from apps.cloud.stem_source import STEM_HUB_UNREACHABLE, StemSourceError

pytestmark = pytest.mark.requirement("STEM-11747")
LOGGER = "apps.cloud.stem_hydration"


class _FakeStems:
    def __init__(self, states: dict[str, str], *, floor: str | None = None, raises: Exception | None = None) -> None:
        self.states = states
        self.floor = floor
        self.raises = raises
        self.calls: list[str] = []

    def hydrate(self, sid: str) -> HydrationOutcome:
        self.calls.append(sid)
        if self.raises is not None:
            raise self.raises
        self.states[sid] = "local"
        return HydrationOutcome(sid, "hydrated")

    def deps(self) -> PrefetchDeps:
        return PrefetchDeps(
            state_of=lambda sid: self.states.get(sid, "none"),
            floor_refusal=lambda: self.floor,
            hydrate=self.hydrate,
        )


class _ManualSubmit:
    def __init__(self) -> None:
        self.queued: list[Callable[[], None]] = []

    def __call__(self, fn: Callable[[], None]) -> None:
        self.queued.append(fn)

    def run_all(self) -> None:
        while self.queued:
            assert len(self.queued) == 1, "if two prefetches are in flight at once then broken"
            self.queued.pop(0)()


def _hub_error() -> StemSourceError:
    return StemSourceError(STEM_HUB_UNREACHABLE, "hub unreachable: connection refused")


def test_prefetch_one_hydrates_a_cloud_head_once() -> None:
    fake = _FakeStems({"X": "cloud"})
    outcome = prefetch_one("X", fake.deps())
    assert outcome.status == "hydrated" and fake.calls == ["X"], "if a cloud head is not hydrated once then broken"


@pytest.mark.parametrize("state", ["local", "none", "fetching"])
def test_prefetch_one_skips_local_and_none_heads(state: str) -> None:
    fake = _FakeStems({"X": state})
    outcome = prefetch_one("X", fake.deps())
    assert outcome.status == "skipped" and outcome.reason and outcome.reason.startswith("not_cloud")
    assert fake.calls == [], "if a non-cloud head is hydrated then broken"


def test_prefetch_one_below_cache_floor_is_skipped_with_a_named_reason(caplog: pytest.LogCaptureFixture) -> None:
    fake = _FakeStems({"X": "cloud"}, floor="free 1 bytes < floor 2 bytes")
    prefetcher = NextUpStemPrefetcher(submit=lambda fn: fn())
    with caplog.at_level(logging.INFO, logger=LOGGER):
        prefetcher.offer("X", fake.deps())
    assert fake.calls == [], "if a prefetch under the cache floor is attempted then broken"
    assert prefetcher.history[-1].reason.startswith("below_cache_floor")
    assert any("below_cache_floor" in r.getMessage() for r in caplog.records)


def test_prefetch_one_never_raises_on_a_hub_error() -> None:
    outcome = prefetch_one("X", _FakeStems({"X": "cloud"}, raises=_hub_error()).deps())
    assert outcome.status == "failed" and outcome.code == STEM_HUB_UNREACHABLE


def test_prefetch_one_never_raises_on_an_unexpected_error(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        outcome = prefetch_one("X", _FakeStems({"X": "cloud"}, raises=RuntimeError("boom")).deps())
    assert outcome.status == "failed" and outcome.code == PREFETCH_FAILED_CODE
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records), "if a defect is silent then broken"


def test_prefetcher_same_head_twice_hydrates_once() -> None:
    fake, submit = _FakeStems({"X": "cloud"}), _ManualSubmit()
    prefetcher = NextUpStemPrefetcher(submit=submit)
    prefetcher.offer("X", fake.deps())
    prefetcher.offer("X", fake.deps())
    submit.run_all()
    prefetcher.offer("X", fake.deps())
    submit.run_all()
    assert fake.calls == ["X"], "if the same head is hydrated twice then broken"


def test_prefetcher_new_head_waits_behind_the_one_in_flight() -> None:
    fake, submit = _FakeStems({"X": "cloud", "Y": "cloud"}), _ManualSubmit()
    prefetcher = NextUpStemPrefetcher(submit=submit)
    prefetcher.offer("X", fake.deps())
    prefetcher.offer("Y", fake.deps())
    assert len(submit.queued) == 1, "if a changed head starts a second prefetch then broken"
    submit.run_all()
    assert fake.calls == ["X", "Y"], "if Y is not prefetched after X then broken"


def test_prefetcher_newest_pending_head_replaces_an_older_one() -> None:
    fake, submit = _FakeStems({"X": "cloud", "Y": "cloud", "Z": "cloud"}), _ManualSubmit()
    prefetcher = NextUpStemPrefetcher(submit=submit)
    for sid in ("X", "Y", "Z"):
        prefetcher.offer(sid, fake.deps())
    submit.run_all()
    assert fake.calls == ["X", "Z"]


def test_prefetcher_hub_error_logs_once_and_next_head_is_tried(caplog: pytest.LogCaptureFixture) -> None:
    fake = _FakeStems({"X": "cloud", "Y": "cloud"}, raises=_hub_error())
    prefetcher = NextUpStemPrefetcher(submit=lambda fn: fn())
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        prefetcher.offer("X", fake.deps())
        prefetcher.offer("X", fake.deps())
        prefetcher.offer("Y", fake.deps())
    assert fake.calls == ["X", "Y"], "if a failed head is retried or the next head skipped then broken"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "if a hub outage logs more than one WARNING then broken"
