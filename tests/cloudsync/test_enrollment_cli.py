"""The dev path's CLI, and the proof it cannot diverge from the HTTP twin.

Contract: ``specs/design_decision_12.md`` section C. the maintainer asked for one
formalized method for adding a machine, with the in-app path landing on the
same mechanism. The load-bearing test here is
:func:`test_cli_and_http_enrollment_write_identical_rows`, which compares the
resulting DATABASE ROWS rather than checking that neither call raised: a CLI
that quietly wrote its own owner row with its own provenance raises nothing
and is exactly the divergence this design exists to prevent.

Regression one-liners:
  - if the CLI and the HTTP endpoint write different rows then broken
  - if a second CLI run does not say "already enrolled" then broken
  - if a subcommand is registered on the parser with no handler then broken
  - if `enroll` with no credential exits zero then broken
  - if a minted grant can start with '-' then broken (argparse eats it)
  - if `--grant-file -` does not enroll from stdin then broken
  - if an empty stdin grant enrolls anything then broken
  - if `fleet --json` omits a count, or misses an unowned machine, then broken
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pytest

from apps.shared.state import machine_identity
from apps.shared.state import schema as state_schema
from apps.sync_hub import (
    client,
    enrollment_credentials,
    maintenance,
    maintenance_enroll,
)
from tests.cloudsync.conftest import HELLO_PATH, free_port
from tests.cloudsync.enrollment_helpers import (
    http_enroll,
    hub_machine_id,
    machine_payload,
    masked_machine,
    masked_owner,
    mint_grant,
    owner_rows,
    read_hub,
)
from tests.cloudsync.enrollment_transport import TestClientTransport


def _enroll_argv(data_dir: Path, *, name: str, token: str) -> list[str]:
    return [
        "enroll",
        "--data-dir",
        str(data_dir),
        "--hub",
        "http://hub.invalid",
        "--name",
        name,
        "--grant",
        token,
    ]


# ----- CLI / HTTP parity ---------------------------------------------------


def test_cli_and_http_enrollment_write_identical_rows(
    enroll_cli_transport: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    enroll_other_spoke_dir: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Same logical enrollment, two entry points, one resulting row shape.

    Both the owner row AND the fleet row are compared, with only the columns
    that MUST differ between two distinct machines masked out. A CLI that
    built a different machine payload, or recorded a different provenance,
    fails here.
    """
    cli_token = mint_grant(enroll_hub_dir)
    assert maintenance.main(
        _enroll_argv(enroll_spoke_dir, name="via-cli", token=cli_token)
    ) == 0
    capsys.readouterr()

    http_enroll(
        enroll_cli_transport,
        enroll_other_spoke_dir,
        name="via-http",
        token=mint_grant(enroll_hub_dir),
    )

    owners = {row["machine_id"]: row for row in owner_rows(enroll_hub_dir)}
    machines = {
        row["machine_id"]: row for row in read_hub(enroll_hub_dir, "machines")
    }
    cli_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    http_id = machine_identity.get_or_create_machine_id(enroll_other_spoke_dir)

    assert set(owners) == {cli_id, http_id}, (
        "both entry points must have produced an owner row"
    )
    assert masked_owner(owners[cli_id]) == masked_owner(owners[http_id])
    assert masked_machine(machines[cli_id]) == masked_machine(machines[http_id])
    assert owners[cli_id]["enrolled_via"] == "grant"


def test_the_cli_reports_the_second_run_as_already_enrolled(
    enroll_cli_transport: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    capsys: pytest.CaptureFixture[str],
):
    argv = _enroll_argv(
        enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    maintenance.main(argv)
    first = capsys.readouterr().out
    maintenance.main(argv)
    second = capsys.readouterr().out

    assert "enrolled" in first
    assert "already enrolled" in second, (
        "a no-op must SAY it was a no-op; printing the success line twice "
        "hides a duplicate from an operator"
    )
    assert len(owner_rows(enroll_hub_dir)) == 1


# ----- the parser contract -------------------------------------------------


def test_every_registered_subcommand_has_a_handler():
    """The dispatch table and the parser must name the same set.

    An invariant, not a list of names: both sides are re-derived, so it
    cannot go stale. A subcommand added to the parser with no handler used to
    be discoverable only by an operator hitting the AssertionError.
    """
    parser = maintenance._parser()
    subparsers = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(subparsers) == 1, "the control: there IS a subparser to read"
    registered = set(subparsers[0].choices)
    assert registered, "the control: it registered at least one subcommand"
    assert registered == (
        set(maintenance.PRINTING_COMMANDS) | maintenance.EXIT_CODE_COMMANDS
    )
    assert {"enroll", "grant", "fleet"} <= registered


def test_enroll_with_no_credential_exits_nonzero(enroll_spoke_dir: Path):
    with pytest.raises(SystemExit) as excinfo:
        maintenance.main(
            ["enroll", "--data-dir", str(enroll_spoke_dir), "--hub", "http://hub.invalid"]
        )
    assert excinfo.value.code != 0


# ----- the grant has to survive a shell ------------------------------------


def test_a_minted_grant_is_always_a_safe_cli_argument(enroll_hub_dir: Path):
    """Regression: a grant must never begin with '-'.

    ``secrets.token_urlsafe`` draws from base64url, so roughly one token in
    sixty-four started with a hyphen and argparse read ``--grant -Xyz`` as a
    MISSING value. It surfaced here as a 2-in-40 flake; on a real machine it
    is an operator reading an argparse error that says nothing about
    enrollment. Fixed at the mint, so the value is safe as a CLI argument, an
    env var and a URL component by construction rather than usually.
    """
    tokens = [mint_grant(enroll_hub_dir) for _ in range(64)]
    assert all(
        token.startswith(enrollment_credentials.GRANT_TOKEN_PREFIX)
        for token in tokens
    )
    assert not any(token.startswith("-") for token in tokens)


def test_argparse_really_does_reject_a_hyphen_leading_grant(enroll_spoke_dir: Path):
    """The control for the test above, and a causal one.

    If argparse tolerated a hyphen-leading value there would be nothing to
    fix and the assertion above would be decoration. This proves the failure
    mode is real, deterministically, rather than by drawing tokens until one
    happens to start with a hyphen.
    """
    with pytest.raises(SystemExit) as excinfo:
        maintenance.main(
            _enroll_argv(enroll_spoke_dir, name="x", token="-leading-hyphen-token")
        )
    assert excinfo.value.code != 0


# ----- keeping the grant out of the process table --------------------------


def test_the_grant_can_be_read_from_stdin_instead_of_argv(
    enroll_cli_transport: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """``--grant-file -`` enrolls, with the token never appearing in argv.

    A token passed as an argument is readable from ``/proc`` by any other
    local process while the call runs, and lands in shell history afterwards.
    On nucbox-wsl -- the machine this command exists for -- that is the
    joining host itself.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(mint_grant(enroll_hub_dir) + "\n"))
    argv = [
        "enroll",
        "--data-dir",
        str(enroll_spoke_dir),
        "--hub",
        "http://hub.invalid",
        "--name",
        "nucbox-wsl",
        "--grant-file",
        "-",
    ]
    assert maintenance.main(argv) == 0
    assert "enrolled" in capsys.readouterr().out
    rows = owner_rows(enroll_hub_dir)
    assert len(rows) == 1
    assert rows[0]["machine_id"] == machine_identity.get_or_create_machine_id(
        enroll_spoke_dir
    )
    assert not any(
        arg.startswith(enrollment_credentials.GRANT_TOKEN_PREFIX) for arg in argv
    ), "the token must not be in the argument vector"


def test_an_empty_stdin_grant_fails_loudly_rather_than_enrolling(
    enroll_cli_transport: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The control: the same path with nothing on stdin must refuse, not pass.

    Without it the test above is equally satisfied by a reader that ignores
    stdin entirely and an endpoint that enrolls anybody.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
    with pytest.raises(ValueError) as excinfo:
        maintenance.main(
            [
                "enroll",
                "--data-dir",
                str(enroll_spoke_dir),
                "--hub",
                "http://hub.invalid",
                "--grant-file",
                "-",
            ]
        )
    assert "empty" in str(excinfo.value)
    assert owner_rows(enroll_hub_dir) == []


def test_the_two_grant_sources_are_mutually_exclusive(enroll_spoke_dir: Path):
    """Both at once is refused by the parser, so no precedence rule exists."""
    with pytest.raises(SystemExit) as excinfo:
        maintenance.main(
            [
                "enroll",
                "--data-dir",
                str(enroll_spoke_dir),
                "--hub",
                "http://hub.invalid",
                "--grant",
                "odjenr_x",
                "--grant-file",
                "-",
            ]
        )
    assert excinfo.value.code != 0


# ----- the fleet readout ---------------------------------------------------


def test_the_cli_fleet_readout_counts_unowned_machines(
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    capsys: pytest.CaptureFixture[str],
):
    enroll_hub.post(
        HELLO_PATH,
        {
            "machine": machine_payload(enroll_spoke_dir, name="never-enrolled"),
            "schema_version": state_schema.SCHEMA_VERSION,
            "machines": [],
        },
    )
    assert maintenance.main(["fleet", "--data-dir", str(enroll_hub_dir), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)

    assert printed["unowned"] == 2, (
        "the hub itself and the machine that only said hello are both unowned"
    )
    assert printed["owned"] == 0
    assert printed["foreign"] == 0
    assert printed["hub_machine_id"] == hub_machine_id(enroll_hub_dir)
    assert {m["name"] for m in printed["machines"]} >= {"never-enrolled"}


def test_the_fleet_readout_counts_an_enrolled_machine_as_owned(
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    capsys: pytest.CaptureFixture[str],
):
    """The control for the count above: the numbers have to MOVE."""
    http_enroll(
        enroll_hub, enroll_spoke_dir, name="nucbox-wsl", token=mint_grant(enroll_hub_dir)
    )
    maintenance.main(["fleet", "--data-dir", str(enroll_hub_dir), "--json"])
    printed = json.loads(capsys.readouterr().out)
    assert printed["owned"] == 1
    assert printed["unowned"] == 1, "the hub has no owner of its own yet"


# ----- the DEV path over a real socket --------------------------------------


def test_the_cli_enrolls_end_to_end_over_a_real_socket(
    enroll_live_hub: str,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] the enroll CLI cannot join a machine over real HTTP then the dev
    path is not proved end to end, [else stop].

    Codex review, PR #1648, P1 BLOCKING. Every other test in this module
    monkeypatches ``maintenance_enroll._transport_for`` to the in-process
    hub, so none of them executes ``HttpTransport``, opens a socket, or
    exercises real request serialization -- they could all stay green while
    the deployed CLI was broken at exactly the layer an operator hits first.

    This test deliberately requests NO transport fixture. It runs
    ``python -m apps.sync_hub enroll`` against a uvicorn server on the
    loopback, so the CLI builds its own production transport and the bytes
    are real. The negative control below is what makes that claim safe: an
    unreachable URL must fail, or this test would pass without the server
    doing anything.
    """
    assert not hasattr(maintenance_enroll._transport_for, "__wrapped__"), (
        "control: this test must drive the real transport factory, so it "
        "fails loudly if a fixture ever patches it out from under us"
    )

    token = mint_grant(enroll_hub_dir)
    argv = [
        "enroll",
        "--data-dir",
        str(enroll_spoke_dir),
        "--hub",
        enroll_live_hub,
        "--name",
        "nucbox-wsl",
        "--grant",
        token,
    ]
    assert maintenance.main(argv) == 0, capsys.readouterr().err
    capsys.readouterr()

    spoke_id = machine_identity.get_or_create_machine_id(enroll_spoke_dir)
    owners = {row["machine_id"]: row for row in owner_rows(enroll_hub_dir)}
    assert spoke_id in owners, (
        "the machine the CLI enrolled over HTTP is not on the hub, so the "
        "call reported success without the row it claims to have written"
    )
    assert owners[spoke_id]["enrolled_via"] == "grant"
    assert owners[spoke_id]["hub_machine_id"] == hub_machine_id(enroll_hub_dir)


def test_the_cli_reports_a_hub_it_cannot_reach(
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] enrolling against a dead hub succeeds then the live test above
    proves nothing, [else stop].

    The negative control for
    :func:`test_the_cli_enrolls_end_to_end_over_a_real_socket`: same CLI,
    same real transport, same valid grant, at a port with nothing behind it.
    Without this, a live test that silently stopped talking to its server
    would keep passing.

    It asserts a RAISE, not an exit code, because that is what ``enroll``
    really does: ``main`` hands ``sync`` and ``status`` their own codes and
    lets every other subcommand propagate, so an unreachable hub leaves the
    process on a traceback naming the URL rather than a bare exit line. That
    is the loud failure the house rules ask for, and pinning the behavior
    that exists beats asserting one that does not.
    """
    dead_port = free_port()
    argv = [
        "enroll",
        "--data-dir",
        str(enroll_spoke_dir),
        "--hub",
        f"http://127.0.0.1:{dead_port}",
        "--name",
        "nowhere",
        "--grant",
        mint_grant(enroll_hub_dir),
    ]
    with pytest.raises(client.SyncTransportError, match=str(dead_port)):
        maintenance.main(argv)
    assert not owner_rows(enroll_hub_dir), (
        "and it must not have written an owner row on the way past"
    )
