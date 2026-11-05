"""LINKBUILD-01: the engine compares the partner's build and Link rules, and both laptops see it
(FE-logic audit #6646 row 65: nothing compared peer builds, so two laptops could run different
take-over or ON AIR rules in silence).

- [if] the partner's Link rules version differs [then] the verdict is different_rules with a
  message naming both versions [else two rule sets decide in silence]
- [if] the rules match but the commit differs [then] the verdict is different_build (information,
  not a warning) (control: a sha difference alone is never called a rules clash)
- [if] the partner's answer carries no stamp (an older build) [then] the verdict is unknown, never
  same
- [if] both stamps match [then] the verdict is same (positive control)
- [if] this engine cannot state its own commit [then] its stamp says so (git_sha None with the
  reason), never a guess

[if] peer build or Link rules comparison regresses [then] fail naming the verdict, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.b2b_session import peer_build

pytestmark = [pytest.mark.requirement("LINKBUILD-01")]


def _stamp(rules: int = peer_build.LINK_RULES_VERSION, sha: str | None = "abc1234", app_version: str | None = "1.0.0") -> dict[str, Any]:
    return {"link_rules": rules, "git_sha": sha, "app_version": app_version, "identity_error": None}


def test_a_rules_difference_is_a_warning_naming_both_versions() -> None:
    verdict = peer_build.compare_builds(_stamp(rules=2), _stamp(rules=3))
    assert verdict["verdict"] == "different_rules"
    assert "2" in verdict["message"] and "3" in verdict["message"]


def test_a_commit_difference_alone_is_information_not_a_rules_clash() -> None:
    verdict = peer_build.compare_builds(_stamp(sha="abc1234"), _stamp(sha="def5678"))
    assert verdict["verdict"] == "different_build"
    assert "abc1234" in verdict["message"] and "def5678" in verdict["message"]


@pytest.mark.parametrize("theirs", [None, {}, {"link_rules": "1"}, {"link_rules": True}, "x"])
def test_no_usable_stamp_is_unknown_never_same(theirs: Any) -> None:
    verdict = peer_build.compare_builds(_stamp(), theirs)
    assert verdict["verdict"] == "unknown"
    assert verdict["theirs"] is None


def test_matching_stamps_are_same() -> None:
    assert peer_build.compare_builds(_stamp(), _stamp())["verdict"] == "same"


def test_an_unknown_commit_on_either_side_is_not_called_a_build_difference() -> None:
    assert peer_build.compare_builds(_stamp(sha=None), _stamp(sha="def5678"))["verdict"] == "same"


def test_a_stamp_carries_a_commit_or_the_reason_never_both_or_neither() -> None:
    failed = peer_build.build_stamp(git_sha=None, app_version=None, identity_error="manifest missing")
    assert failed["git_sha"] is None and failed["identity_error"] == "manifest missing"
    assert failed["link_rules"] == peer_build.LINK_RULES_VERSION, "the rules version is code, always known"
    for sha, error in ((None, None), ("abc1234", "manifest missing")):
        with pytest.raises(ValueError):
            peer_build.build_stamp(git_sha=sha, app_version=None, identity_error=error)
