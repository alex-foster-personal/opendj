"""Billing-API ledger acceptance tests (#2506 / DEVOPS-11)."""

import json
from datetime import UTC, datetime

from scripts.ci_cost_billing import (
    MAX_BILLING_API_CALLS,
    build_billing_ledger,
)
from scripts.ci_cost_ledger import main as ledger_main


def _usage_row(
    day: str,
    sku: str,
    quantity: int,
    net_amount: float,
    repo: str = "music-dj-tools",
) -> dict:
    return {
        "date": day,
        "product": "Actions",
        "sku": sku,
        "quantity": quantity,
        "unitType": "minutes",
        "pricePerUnit": 0.006,
        "grossAmount": net_amount,
        "discountAmount": 0.0,
        "netAmount": net_amount,
        "repositoryName": repo,
    }


def _fresh_items(*repo_rows: dict) -> list[dict]:
    """Repo rows plus a recent row elsewhere so the billing feed looks fresh."""
    return [
        *repo_rows,
        _usage_row("2026-09-14", "Codespaces", 1, 0.0, repo="other-repo"),
    ]


def _write_billing_fixture(path, items: list[dict], *, now: str | None = None) -> None:
    payload: dict = {"usageItems": items}
    if now:
        payload["now"] = now
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_billing_ledger_makes_at_most_five_api_calls(monkeypatch):
    from scripts import ci_cost_ledger

    calls = []

    def _fetch(user, year, month, token):
        calls.append((user, year, month))
        return _fresh_items(_usage_row("2026-09-02", "Actions Linux", 10, 0.06)), 1

    monkeypatch.setattr(ci_cost_ledger, "fetch_billing_usage", _fetch)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci_cost_ledger",
            "--repository",
            "maintainer/music-dj-tools",
            "--token",
            "token",
            "--month",
            "2026-09",
            "--report-file",
            "/dev/null",
        ],
    )

    assert ledger_main() == 0
    assert len(calls) == 1
    assert len(calls) <= MAX_BILLING_API_CALLS


def test_billing_report_lists_minutes_and_net_per_sku(tmp_path, monkeypatch):
    fixture = tmp_path / "billing.json"
    _write_billing_fixture(
        fixture,
        _fresh_items(
            _usage_row("2026-09-01", "Actions Linux", 20047, 111.32),
            _usage_row("2026-09-02", "Actions macOS 3-core", 511, 28.64),
            _usage_row("2026-09-02", "Actions Windows", 3, 0.03),
        ),
        now="2026-09-14T12:00:00+00:00",
    )
    report_file = tmp_path / "report.md"
    json_file = tmp_path / "summary.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci_cost_ledger",
            "--repository",
            "maintainer/music-dj-tools",
            "--billing-json",
            str(fixture),
            "--month",
            "2026-09",
            "--allowance-minutes",
            "2000",
            "--report-file",
            str(report_file),
            "--json-file",
            str(json_file),
        ],
    )

    assert ledger_main() == 0
    report = report_file.read_text()
    summary = json.loads(json_file.read_text())
    assert "Actions Linux" in report
    assert "20047" in report
    assert "$111.32" in report
    assert "Actions macOS 3-core" in report
    assert "511" in report
    assert "$28.64" in report
    assert "Measured through: **2026-09-02**" in report
    assert summary["measured_at"] == "2026-09-02"
    assert summary["source"] == "billing_usage"


def test_stale_billing_feed_is_unknown_without_numbers(tmp_path, monkeypatch):
    fixture = tmp_path / "billing.json"
    _write_billing_fixture(
        fixture,
        [_usage_row("2026-08-01", "Actions Linux", 100, 1.0)],
        now="2026-09-14T12:00:00+00:00",
    )
    json_file = tmp_path / "summary.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci_cost_ledger",
            "--repository",
            "maintainer/music-dj-tools",
            "--billing-json",
            str(fixture),
            "--month",
            "2026-09",
            "--report-file",
            str(tmp_path / "report.md"),
            "--json-file",
            str(json_file),
        ],
    )

    assert ledger_main() == 0
    summary = json.loads(json_file.read_text())
    report = (tmp_path / "report.md").read_text()
    assert summary["state"] == "UNKNOWN"
    assert summary["allowance_used"] == 0
    assert "48" in summary["unknown_reason"] or "old" in summary["unknown_reason"]
    assert "UNTRUSTED" in report
    assert "2000 min" not in report or "Allowance used" not in report


def test_no_repo_rows_for_month_is_unknown(tmp_path, monkeypatch):
    fixture = tmp_path / "billing.json"
    _write_billing_fixture(
        fixture,
        [_usage_row("2026-09-13", "Codespaces", 1, 0.0, repo="other-repo")],
        now="2026-09-14T12:00:00+00:00",
    )
    json_file = tmp_path / "summary.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci_cost_ledger",
            "--repository",
            "maintainer/music-dj-tools",
            "--billing-json",
            str(fixture),
            "--month",
            "2026-09",
            "--report-file",
            str(tmp_path / "report.md"),
            "--json-file",
            str(json_file),
        ],
    )

    assert ledger_main() == 0
    summary = json.loads(json_file.read_text())
    assert summary["state"] == "UNKNOWN"
    assert "no billing rows" in summary["unknown_reason"]


def test_pre_cutover_hosted_rows_pass(tmp_path, monkeypatch):
    fixture = tmp_path / "billing.json"
    _write_billing_fixture(
        fixture,
        _fresh_items(
            _usage_row("2026-09-01", "Actions Linux", 100, 1.0),
            _usage_row("2026-09-03", "Actions macOS 3-core", 10, 0.62),
        ),
        now="2026-09-14T12:00:00+00:00",
    )
    json_file = tmp_path / "summary.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci_cost_ledger",
            "--repository",
            "maintainer/music-dj-tools",
            "--billing-json",
            str(fixture),
            "--month",
            "2026-09",
            "--allowance-minutes",
            "3000",
            "--report-file",
            str(tmp_path / "report.md"),
            "--json-file",
            str(json_file),
        ],
    )

    assert ledger_main() == 0
    summary = json.loads(json_file.read_text())
    assert summary["state"] == "OK"
    assert summary["allowance_used"] == 200


def test_post_cutover_macos_row_fails_loudly(tmp_path, monkeypatch):
    fixture = tmp_path / "billing.json"
    _write_billing_fixture(
        fixture,
        _fresh_items(
            _usage_row("2026-09-01", "Actions Linux", 10, 0.06),
            _usage_row("2026-09-05", "Actions macOS 3-core", 5, 0.31),
        ),
        now="2026-09-14T12:00:00+00:00",
    )
    json_file = tmp_path / "summary.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci_cost_ledger",
            "--repository",
            "maintainer/music-dj-tools",
            "--billing-json",
            str(fixture),
            "--month",
            "2026-09",
            "--allowance-minutes",
            "3000",
            "--report-file",
            str(tmp_path / "report.md"),
            "--json-file",
            str(json_file),
        ],
    )

    assert ledger_main() == 1
    summary = json.loads(json_file.read_text())
    report = (tmp_path / "report.md").read_text()
    assert summary["state"] == "HOSTED"
    assert summary["hosted_violations"][0]["sku"] == "Actions macOS 3-core"
    assert summary["hosted_violations"][0]["day"] == "2026-09-05"
    assert "HOSTED BILLING AFTER CUTOVER" in report


def test_build_billing_ledger_freshness_uses_global_latest():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    stale = build_billing_ledger(
        [_usage_row("2026-08-01", "Actions Linux", 1, 0.01)],
        repository="maintainer/music-dj-tools",
        month="2026-09",
        api_calls=1,
        now=now,
    )
    assert stale.unknown_reason is not None

    fresh = build_billing_ledger(
        _fresh_items(_usage_row("2026-09-02", "Actions Linux", 1, 0.01)),
        repository="maintainer/music-dj-tools",
        month="2026-09",
        api_calls=1,
        now=now,
    )
    assert fresh.unknown_reason is None
