"""OBS-01 Part 1: stable error ids are a hash of source site + message class.

Regression lines:
  - if two events at the same site with the same message class mint different
    ids, then broken
  - if a volatile token (uuid, hex, integer) in the message changes the id,
    then broken
  - if a different source site with the same message mints the same id, then
    broken

[if] stable_error_id hashes match site/class or differ by site [then] ids differ, [else stop].
"""

from __future__ import annotations

import pytest

from apps.shared.telemetry.error_id import classify_message, stable_error_id

pytestmark = pytest.mark.requirement("OBS-01")


def test_same_site_and_message_class_mint_the_same_id() -> None:
    """if the site and class match then the id is byte-identical."""
    first = stable_error_id(
        source_site="apps.engine_core.jobs.runner:412",
        message="job 17 failed: stem decode",
    )
    second = stable_error_id(
        source_site="apps.engine_core.jobs.runner:412",
        message="job 17 failed: stem decode",
    )
    assert first == second
    assert first.startswith("eid-")
    assert len(first) == 16, "eid- plus 12 hex chars"


def test_volatile_tokens_in_the_message_do_not_change_the_id() -> None:
    """if a uuid, hex sha, or integer changes then the class (and id) stay put."""
    site = "apps.webui.server.routes.client_errors:capture"
    base = stable_error_id(
        source_site=site,
        message="job 17 failed for 0123456789abcdef0123456789abcdef01234567",
    )
    uuid_swap = stable_error_id(
        source_site=site,
        message="job 99 failed for 11111111-2222-4333-8444-555555555555",
    )
    assert base == uuid_swap
    assert classify_message("job 17 failed") == classify_message("job 99 failed")


def test_a_different_source_site_mints_a_different_id() -> None:
    """if the raising site changes then the id changes, even with the same text."""
    message = "AudioWorklet is unavailable so Signalsmith cannot start"
    engine = stable_error_id(source_site="engine:warning_log", message=message)
    client = stable_error_id(source_site="client:ui-error:/performance", message=message)
    assert engine != client
