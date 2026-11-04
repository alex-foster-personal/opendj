"""Sha256 pin and structural claims for ci/vendor/runners.v1.yaml (issue #7002).

v1 covers only the 46 Ansible runners (agentbox, agbox2, agbox3). Outside v1:
nucbox-wsl x30, bifrost1 x9, megamac-vm x4, and the retired agentbox.
"""

from __future__ import annotations

import hashlib

from tests.scripts.vendored_runners import V1_PATH, V1_SHA256, document, seats

_EXPECTED_POOLS = {
    "agentbox",
    "e2e",
    "linux-light",
    "main-fix",
    "mq",
    "pytest",
    "trunk",
    "trunk-agentbox",
}

_MQ_SEATS = {
    "agbox2-9",
    "agbox2-10",
    "agbox2-11",
    "agbox2-12",
    "agbox3-9",
    "agbox3-10",
    "agbox3-11",
    "agbox3-12",
}


def test_vendored_runners_v1_sha256_matches_pin() -> None:
    assert hashlib.sha256(V1_PATH.read_bytes()).hexdigest() == V1_SHA256


def test_vendored_runners_v1_schema_and_top_level_keys() -> None:
    doc = document()
    assert doc["schema"] == 1
    assert "pools" in doc and "hosts" in doc


def test_vendored_runners_v1_pool_names() -> None:
    assert set(document()["pools"]) == _EXPECTED_POOLS


def test_vendored_runners_v1_hosts_and_runner_count() -> None:
    doc = document()
    host_names = {h["name"] for h in doc["hosts"]}
    assert host_names == {"agentbox", "agbox2", "agbox3"}
    assert all(not h["cordon"] for h in doc["hosts"])
    runner_count = sum(len(h["runners"]) for h in doc["hosts"])
    assert runner_count == 46


def test_vendored_runners_v1_mq_seats() -> None:
    assert set(seats("mq")) == _MQ_SEATS


def test_vendored_runners_v1_main_fix_seats_are_partial() -> None:
    """v1 lists agentbox-14/15 only; the live four-seat pool includes nucbox outside v1."""
    assert set(seats("main-fix")) == {"agentbox-14", "agentbox-15"}


def test_vendored_runners_v1_excludes_nucbox_bifrost_megamac() -> None:
    doc = document()
    for host in doc["hosts"]:
        assert "nucbox" not in host["name"]
        assert "bifrost" not in host["name"]
        assert "megamac" not in host["name"]
        for runner in host["runners"]:
            assert "nucbox" not in runner["name"]
            assert "bifrost" not in runner["name"]
            assert "megamac" not in runner["name"]
