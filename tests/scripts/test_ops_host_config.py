"""Ops-script host names come from explicit env config, defaults unchanged (issue #5639, batch 4c).

Stage 1 of moving hardcoded host names to documented variables: set, each
variable names the hosts; unset or blank keeps today's house values, so no
machine changes behaviour before it sets them (ADR-0317 disposition on #5639).
sync_untracked's MDT_SYNC_REMOTE_CHECKOUTS is covered in test_sync_untracked.py.

Regression one-liners:
  - if MDT_SINK_TRIAGE_LOCAL_NAME / MDT_SINK_TRIAGE_SSH_HOSTS unset or blank are not nucbox / silver,air then broken
  - if set, they do not name sink_triage's local and ssh sources then broken
  - if MDT_SOL_REMOTE_SEAT / MDT_SOL_REMOTE_SEAT_HOSTNAMES unset or blank are not nucbox-wsl / NucBoxEVO-X2 then broken
  - if set, they do not name sol_review's remote seat and where it runs locally then broken
  - if a value that is not one ssh alias reaches ssh argv then broken
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import sink_triage, sol_review
from scripts.review_gh import TriageError

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_NAMES = (
    sink_triage.LOCAL_SOURCE_NAME_ENV,
    sink_triage.SSH_SOURCES_ENV,
    sol_review.REMOTE_SEAT_ENV,
    sol_review.REMOTE_SEAT_HOSTNAMES_ENV,
)
BAD_ALIASES = ["-oProxyCommand=x", "two words", "host;id", "a,b=c"]


@pytest.mark.parametrize("value", [None, "", " ", " , "], ids=["unset", "blank", "spaces", "commas"])
def test_sink_triage_sources_raise_when_unset_or_blank(value: str | None) -> None:
    """[if] unset or blank sink-triage names do not raise [then] fail, [else stop]."""
    environ = (
        {}
        if value is None
        else {sink_triage.LOCAL_SOURCE_NAME_ENV: value.replace(",", ""), sink_triage.SSH_SOURCES_ENV: value}
    )
    with pytest.raises(ValueError, match="MDT_SINK_TRIAGE"):
        sink_triage.source_names(environ)


def test_sink_triage_override_names_every_source(tmp_path: Path) -> None:
    """[if] set sink-triage names do not name the local and ssh sources [then] fail, [else stop]."""
    environ = {
        sink_triage.LOCAL_SOURCE_NAME_ENV: " triage-host ",
        sink_triage.SSH_SOURCES_ENV: "mac-a, mac-b.example.ts.net,",
    }
    sources = sink_triage.default_sources(tmp_path, environ)
    assert [(s.name, s.mode) for s in sources] == [
        ("triage-host", "local"),
        ("mac-a", "ssh"),
        ("mac-b.example.ts.net", "ssh"),
    ]


@pytest.mark.parametrize("bad", BAD_ALIASES)
@pytest.mark.parametrize("variable", ["MDT_SINK_TRIAGE_LOCAL_NAME", "MDT_SINK_TRIAGE_SSH_HOSTS"])
def test_sink_triage_refuses_a_name_that_is_not_an_ssh_alias(variable: str, bad: str) -> None:
    """[if] a non-alias reaches sink-triage's ssh argv [then] fail, [else stop]."""
    if variable == sink_triage.SSH_SOURCES_ENV and "," in bad:
        bad = bad.split(",")[1]
    base = {
        sink_triage.LOCAL_SOURCE_NAME_ENV: "nucbox",
        sink_triage.SSH_SOURCES_ENV: "silver,air",
    }
    with pytest.raises(ValueError, match=variable):
        sink_triage.source_names({**base, variable: bad})


@pytest.mark.parametrize("value", [None, "", " "], ids=["unset", "blank", "spaces"])
def test_sol_remote_seat_raises_when_unset_or_blank(value: str | None) -> None:
    """[if] unset or blank seat config does not raise [then] fail, [else stop]."""
    environ = {} if value is None else {sol_review.REMOTE_SEAT_ENV: value, sol_review.REMOTE_SEAT_HOSTNAMES_ENV: value}
    with pytest.raises(TriageError, match="MDT_SOL_REMOTE"):
        sol_review.remote_seat(environ)
        sol_review.remote_seat_hostnames(environ)


def test_sol_remote_seat_override_names_the_seat_and_its_hosts() -> None:
    """[if] set seat config does not name the seat and its hostnames [then] fail, [else stop]."""
    environ = {
        sol_review.REMOTE_SEAT_ENV: " review-seat ",
        sol_review.REMOTE_SEAT_HOSTNAMES_ENV: "SeatHost, seat-host.local",
    }
    assert sol_review.remote_seat(environ) == "review-seat"
    assert sol_review.remote_seat_hostnames(environ) == frozenset({"SeatHost", "seat-host.local"})


@pytest.mark.parametrize("bad", BAD_ALIASES[:3])
@pytest.mark.parametrize("variable", ["MDT_SOL_REMOTE_SEAT", "MDT_SOL_REMOTE_SEAT_HOSTNAMES"])
def test_sol_seat_config_refuses_a_name_that_is_not_an_ssh_alias(variable: str, bad: str) -> None:
    """[if] a non-alias seat reaches ssh argv [then] fail, [else stop]."""
    reader = sol_review.remote_seat if variable == sol_review.REMOTE_SEAT_ENV else sol_review.remote_seat_hostnames
    with pytest.raises(TriageError, match=variable):
        reader({variable: bad})


_PROBE = """
import json, sys, tempfile
from pathlib import Path
from scripts import sink_triage, sol_review
try:
    print(json.dumps({
        "sink_sources": [s.name for s in sink_triage.default_sources(Path(tempfile.gettempdir()))],
        "seats": list(sol_review.configured_seats()),
        "local_seat_hosts": {k: sorted(v) for k, v in sol_review.local_seat_hosts().items()},
    }))
except (ValueError, Exception) as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
"""


@pytest.mark.parametrize(
    ("overrides", "expected", "expect_ok"),
    [
        ({}, {}, False),
        (
            {
                "MDT_SINK_TRIAGE_LOCAL_NAME": "triage-host",
                "MDT_SINK_TRIAGE_SSH_HOSTS": "mac-a",
                "MDT_SOL_REMOTE_SEAT": "review-seat",
                "MDT_SOL_REMOTE_SEAT_HOSTNAMES": "SeatHost",
            },
            {
                "sink_sources": ["triage-host", "mac-a"],
                "seats": ["local", "review-seat"],
                "local_seat_hosts": {"review-seat": ["SeatHost"]},
            },
            True,
        ),
    ],
    ids=["unset", "override"],
)
def test_the_scripts_read_the_process_environment(
    overrides: dict[str, str], expected: dict[str, object], expect_ok: bool
) -> None:
    """[if] the module-level config ignores the process env [then] fail, [else stop]."""
    env = {k: v for k, v in os.environ.items() if k not in ENV_NAMES}
    env.update(overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if not expect_ok:
        assert proc.returncode != 0
        assert "MDT_" in (proc.stderr or proc.stdout)
        return
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == expected
