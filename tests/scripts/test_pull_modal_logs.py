"""Archive-layer tests for scripts/pull_modal_logs.py (C2, commit 9f323683).

The two functions that shell out to the `modal` CLI (``list_apps``,
``fetch_app_logs``) need live Modal auth and are deliberately NOT exercised
here -- see the audit note in the PR body. Everything below is the part that
decides WHERE a pulled log lands and WHETHER it has already been pulled,
which is the whole reason the script is safe to re-run on a timer.

Regression lines:
  - if created_date stops taking the date half of modal's created_at then a
    day's logs scatter across differently-named files
  - if archive_path drops the app description or the date then two apps
    overwrite each other's archive
  - if already_pulled matches on a bare app_id substring then a longer app id
    sharing a prefix is wrongly reported as archived and never pulled
  - if append_block truncates instead of appending then re-running the pull
    destroys the earlier blocks it was written to preserve
  - if an app with no log lines writes nothing then "pulled and empty" is
    indistinguishable from "never pulled"
"""

from __future__ import annotations

import pytest

from scripts import pull_modal_logs as pml


def _app(app_id: str = "ap-AAA", description: str = "mdt-roformer") -> pml.ModalApp:
    return pml.ModalApp(
        app_id=app_id,
        description=description,
        state="stopped",
        created_at="2026-07-31 06:13:16+01:00",
        stopped_at="2026-07-31 06:41:02+01:00",
    )


# ----- bucketing ------------------------------------------------------------


def test_created_date_is_the_date_half_of_modal_created_at() -> None:
    assert _app().created_date == "2026-07-31"


def test_archive_path_buckets_by_description_then_day(tmp_path) -> None:
    path = pml.archive_path(tmp_path, _app())
    assert path == tmp_path / "state" / "modal-logs" / "mdt-roformer" / "2026-07-31.log"


def test_two_apps_same_day_different_names_do_not_collide(tmp_path) -> None:
    a = pml.archive_path(tmp_path, _app(description="mdt-roformer"))
    b = pml.archive_path(tmp_path, _app(description="mdt-demucs-ab"))
    assert a != b


# ----- idempotency ----------------------------------------------------------


def test_already_pulled_is_false_when_no_archive_exists(tmp_path) -> None:
    assert pml.already_pulled(tmp_path / "nope.log", "ap-AAA") is False


def test_append_then_already_pulled_reports_true(tmp_path) -> None:
    app = _app()
    path = pml.archive_path(tmp_path, app)
    pml.append_block(path, app, "line one\nline two\n")
    assert pml.already_pulled(path, app.app_id) is True


def test_already_pulled_does_not_match_a_shorter_app_id_sharing_a_prefix(tmp_path) -> None:
    """An archived `ap-12` must NOT make `ap-1` look already-pulled.

    This is the direction that silently loses a log: without the delimiter
    after the id, a substring search says "already archived" for an app that
    was never fetched, and its logs expire unpulled.
    """
    archived = _app(app_id="ap-12")
    path = pml.archive_path(tmp_path, archived)
    pml.append_block(path, archived, "x\n")
    assert pml.already_pulled(path, "ap-12") is True
    assert pml.already_pulled(path, "ap-1") is False


def test_append_is_append_only_across_apps(tmp_path) -> None:
    first, second = _app(app_id="ap-1"), _app(app_id="ap-2")
    path = pml.archive_path(tmp_path, first)
    pml.append_block(path, first, "first run log\n")
    pml.append_block(path, second, "second run log\n")
    text = path.read_text()
    assert "first run log" in text and "second run log" in text
    assert text.count(pml.BLOCK_HEADER_PREFIX) == 2


def test_empty_log_still_records_the_attempt(tmp_path) -> None:
    app = _app()
    path = pml.archive_path(tmp_path, app)
    pml.append_block(path, app, "")
    text = path.read_text()
    assert "(no log lines returned)" in text
    assert pml.already_pulled(path, app.app_id) is True


def test_header_carries_the_provenance_fields(tmp_path) -> None:
    app = _app()
    path = pml.archive_path(tmp_path, app)
    pml.append_block(path, app, "body\n")
    header = path.read_text().splitlines()[0]
    for field in ("app_id=ap-AAA", "description=mdt-roformer", "state=stopped",
                  "created_at=2026-07-31", "pulled_at="):
        assert field in header, header


def test_body_always_ends_newline_terminated(tmp_path) -> None:
    """A block without a trailing newline would weld into the next header."""
    app = _app()
    path = pml.archive_path(tmp_path, app)
    pml.append_block(path, app, "no trailing newline")
    assert path.read_text().endswith("\n")
    pml.append_block(path, _app(app_id="ap-B"), "second\n")
    lines = path.read_text().splitlines()
    assert sum(1 for ln in lines if ln.startswith(pml.BLOCK_HEADER_PREFIX)) == 2


# ----- defaults -------------------------------------------------------------


def test_default_prefix_scopes_the_pull_to_this_repos_apps() -> None:
    assert pml.DEFAULT_APP_PREFIX == "mdt-"


def test_require_modal_cli_raises_rather_than_returning_nothing(monkeypatch) -> None:
    monkeypatch.setattr(pml.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        pml._require_modal_cli()
