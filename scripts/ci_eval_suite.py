#!/usr/bin/env python3
"""Run an occasional, disposable ten-PR GitHub Actions evaluation campaign.

The tool separates planning from live mutation. ``plan`` only writes a local
manifest. ``prepare`` and ``submit`` require both ``--execute`` and an exact
target-repository confirmation. ``collect`` is read-only and produces raw JSON
plus a Markdown summary for an LLM operator to assess.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.ci_eval_collection import (
        CampaignError,
        collect_campaign,
        render_campaign_report,
    )
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.ci_eval_suite") from None
    raise

CASE_COUNT = 10
EXPECTED_WORKFLOWS = ("CI", "Build docs")
TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,47}$")


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    """Small subprocess seam so live GitHub operations remain testable."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> CommandResult:
        print(f"$ {shlex.join(args)}", file=sys.stderr)
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        result = CommandResult(completed.returncode, completed.stdout, completed.stderr)
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise CampaignError(f"command failed ({result.returncode}): {detail}")
        return result


def utc_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz").lower()


def validate_repository(value: str, *, field: str) -> str:
    if not TARGET_RE.fullmatch(value):
        raise CampaignError(f"{field} must be an owner/repository name, got {value!r}")
    return value


def validate_target(source_repository: str, target_repository: str) -> None:
    validate_repository(source_repository, field="source_repository")
    validate_repository(target_repository, field="target_repository")
    if source_repository.lower() == target_repository.lower():
        raise CampaignError("the eval target must never be the source repository")
    target_name = target_repository.split("/", 1)[1].lower().replace("_", "-")
    if "ci-eval" not in target_name:
        raise CampaignError("the disposable target repository name must contain 'ci-eval'")


def validate_ref(value: str) -> str:
    invalid = ("..", "~", "^", ":", "?", "*", "[", "\\")
    if not value or value.startswith(("-", ".")) or value.endswith(("/", ".lock")):
        raise CampaignError(f"unsafe source ref {value!r}")
    if any(token in value for token in invalid) or "//" in value or "@{" in value:
        raise CampaignError(f"unsafe source ref {value!r}")
    return value


def build_campaign(
    *,
    source_repository: str,
    source_ref: str,
    target_repository: str,
    run_id: str,
    workspace: Path,
) -> dict[str, Any]:
    validate_target(source_repository, target_repository)
    validate_ref(source_ref)
    if not RUN_ID_RE.fullmatch(run_id):
        raise CampaignError("run_id must be 3-48 lowercase letters, digits, or hyphens")

    cases = []
    for number in range(1, CASE_COUNT + 1):
        case_id = f"{number:02d}"
        cases.append(
            {
                "id": case_id,
                "branch": f"ci-eval/{run_id}/pr-{case_id}",
                "marker": f".ci-eval/cases/{run_id}-pr-{case_id}.json",
                "title": f"test(ci): eval campaign {run_id} case {case_id}",
            }
        )

    return {
        "schema_version": 1,
        "state": "planned",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "source_repository": source_repository,
        "source_ref": source_ref,
        "target_repository": target_repository,
        "base_branch": source_ref,
        "workspace": str(workspace.resolve()),
        "checkout": str((workspace / "checkout").resolve()),
        "expected_workflows": list(EXPECTED_WORKFLOWS),
        "cases": cases,
        "prs": [],
    }


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        campaign = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"cannot read campaign manifest {path}: {exc}") from exc
    if campaign.get("schema_version") != 1:
        raise CampaignError("unsupported campaign manifest schema")
    validate_target(campaign["source_repository"], campaign["target_repository"])
    if len(campaign.get("cases") or []) != CASE_COUNT:
        raise CampaignError(f"campaign must contain exactly {CASE_COUNT} cases")
    return campaign


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def require_live_confirmation(args: argparse.Namespace, campaign: dict[str, Any]) -> None:
    if not args.execute:
        raise CampaignError("live mutation requires --execute")
    target = campaign["target_repository"]
    if args.confirm_target != target:
        raise CampaignError(f"pass --confirm-target {target!r} exactly")


def plan_command(args: argparse.Namespace) -> int:
    workspace = args.manifest.parent
    campaign = build_campaign(
        source_repository=args.source_repository,
        source_ref=args.source_ref,
        target_repository=args.target_repository,
        run_id=args.run_id or utc_run_id(),
        workspace=workspace,
    )
    if args.manifest.exists() and not args.replace:
        raise CampaignError(f"manifest already exists: {args.manifest}; pass --replace to overwrite")
    write_json(args.manifest, campaign)
    print(json.dumps(campaign, indent=2))
    return 0


def prepare_command(args: argparse.Namespace, runner: CommandRunner) -> int:
    campaign = load_manifest(args.manifest)
    require_live_confirmation(args, campaign)
    target = campaign["target_repository"]
    source = campaign["source_repository"]
    checkout = Path(campaign["checkout"])
    description = f"Disposable CI evaluation mirror [af-evalsuite-ci:{campaign['run_id']}]"

    runner.run(["gh", "auth", "status"])
    existing = runner.run(
        ["gh", "repo", "view", target, "--json", "description,isPrivate"],
        check=False,
    )
    if existing.returncode == 0:
        payload = json.loads(existing.stdout)
        if not args.resume:
            raise CampaignError(f"target repository already exists: {target}; use --resume only for this campaign")
        if payload.get("description") != description or not payload.get("isPrivate"):
            raise CampaignError("existing target is not this campaign's private disposable mirror")
    else:
        runner.run(
            [
                "gh",
                "repo",
                "create",
                target,
                "--private",
                "--disable-wiki",
                "--description",
                description,
            ]
        )

    if checkout.exists():
        if not args.resume:
            raise CampaignError(f"checkout already exists: {checkout}")
    else:
        checkout.parent.mkdir(parents=True, exist_ok=True)
        runner.run(
            [
                "gh",
                "repo",
                "clone",
                source,
                str(checkout),
                "--",
                "--single-branch",
                "--branch",
                campaign["source_ref"],
            ]
        )
        runner.run(["git", "remote", "rename", "origin", "source"], cwd=checkout)
        ssh_url = runner.run(
            ["gh", "repo", "view", target, "--json", "sshUrl", "--jq", ".sshUrl"]
        ).stdout.strip()
        runner.run(["git", "remote", "add", "origin", ssh_url], cwd=checkout)
        runner.run(
            ["git", "push", "-u", "origin", f"HEAD:refs/heads/{campaign['base_branch']}"],
            cwd=checkout,
        )
        runner.run(
            ["gh", "repo", "edit", target, "--default-branch", campaign["base_branch"]]
        )

    campaign["state"] = "prepared"
    campaign["prepared_at"] = datetime.now(UTC).isoformat()
    write_json(args.manifest, campaign)
    print(f"Prepared private mirror {target} at {checkout}")
    return 0


def _remote_branch_sha(runner: CommandRunner, checkout: Path, branch: str) -> str | None:
    result = runner.run(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        cwd=checkout,
    )
    line = result.stdout.strip().splitlines()
    if not line:
        return None
    return line[0].split()[0]


def _existing_pr(runner: CommandRunner, target: str, branch: str) -> dict[str, Any] | None:
    result = runner.run(
        ["gh", "pr", "view", branch, "--repo", target, "--json", "number,url"],
        check=False,
    )
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout)
    return {"number": int(payload["number"]), "url": payload["url"]}


def _ensure_pushed_case(
    runner: CommandRunner,
    campaign: dict[str, Any],
    case: dict[str, Any],
    checkout: Path,
) -> str:
    if _remote_branch_sha(runner, checkout, case["branch"]):
        runner.run(["git", "fetch", "origin", case["branch"]], cwd=checkout)
        runner.run(
            ["git", "switch", "--force-create", case["branch"], "FETCH_HEAD"],
            cwd=checkout,
        )
        return runner.run(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()

    runner.run(
        ["git", "switch", "--force-create", case["branch"], campaign["base_branch"]],
        cwd=checkout,
    )
    write_json(
        checkout / case["marker"],
        {
            "case_id": case["id"],
            "run_id": campaign["run_id"],
            "purpose": "harmless PR fan-out for occasional CI evaluation",
            "expected_workflows": campaign["expected_workflows"],
        },
    )
    runner.run(["git", "add", case["marker"]], cwd=checkout)
    runner.run(
        [
            "git",
            "-c",
            "user.name=AF CI Eval Suite",
            "-c",
            "user.email=ci-eval@users.noreply.github.com",
            "commit",
            "-m",
            case["title"],
        ],
        cwd=checkout,
    )
    head_sha = runner.run(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    runner.run(["git", "push", "-u", "origin", case["branch"]], cwd=checkout)
    return head_sha


def _upsert_pushed_pr_row(
    campaign: dict[str, Any], case: dict[str, Any], head_sha: str
) -> dict[str, Any]:
    prs = campaign.setdefault("prs", [])
    for row in prs:
        if row["case_id"] == case["id"]:
            row["branch"] = case["branch"]
            row["head_sha"] = head_sha
            return row
    row = {"case_id": case["id"], "branch": case["branch"], "head_sha": head_sha}
    prs.append(row)
    return row


def _create_or_reuse_pr(
    runner: CommandRunner, campaign: dict[str, Any], case: dict[str, Any]
) -> tuple[int, str]:
    existing = _existing_pr(runner, campaign["target_repository"], case["branch"])
    if existing:
        return existing["number"], existing["url"]
    body = (
        f"Disposable CI evaluation case `{case['id']}` for `{campaign['run_id']}`.\n\n"
        "No product behavior changes. Keep this PR open until the campaign report is complete."
    )
    created = runner.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            campaign["target_repository"],
            "--base",
            campaign["base_branch"],
            "--head",
            case["branch"],
            "--title",
            case["title"],
            "--body",
            body,
        ]
    )
    pr_url = created.stdout.strip().splitlines()[-1]
    match = re.search(r"/(\d+)$", pr_url)
    if not match:
        raise CampaignError(f"could not parse PR URL from gh output: {pr_url!r}")
    return int(match.group(1)), pr_url


def _fully_submitted_prs(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in prs if row.get("number") and row.get("url")]


def submit_command(args: argparse.Namespace, runner: CommandRunner) -> int:
    campaign = load_manifest(args.manifest)
    require_live_confirmation(args, campaign)
    if campaign.get("state") not in {"prepared", "submitted"}:
        raise CampaignError("prepare the campaign before submitting PRs")
    checkout = Path(campaign["checkout"])
    if not (checkout / ".git").exists():
        raise CampaignError(f"disposable checkout is missing: {checkout}")
    if campaign.get("prs") and not args.resume:
        raise CampaignError("campaign already has submitted PRs; use --resume to continue")

    submitted = {row["case_id"]: row for row in campaign.get("prs") or []}
    for case in campaign["cases"]:
        existing = submitted.get(case["id"])
        if existing and existing.get("number"):
            continue
        head_sha = _ensure_pushed_case(runner, campaign, case, checkout)
        row = _upsert_pushed_pr_row(campaign, case, head_sha)
        submitted[case["id"]] = row
        write_json(args.manifest, campaign)
        number, url = _create_or_reuse_pr(runner, campaign, case)
        row["number"] = number
        row["url"] = url
        write_json(args.manifest, campaign)

    complete = _fully_submitted_prs(campaign.get("prs") or [])
    if len(complete) != CASE_COUNT:
        raise CampaignError(f"expected {CASE_COUNT} submitted PRs, got {len(complete)}")
    campaign["state"] = "submitted"
    campaign["submitted_at"] = datetime.now(UTC).isoformat()
    write_json(args.manifest, campaign)
    print(f"Submitted {CASE_COUNT} PRs to {campaign['target_repository']}")
    return 0


def collect_command(args: argparse.Namespace, runner: CommandRunner) -> int:
    campaign = load_manifest(args.manifest)
    result = collect_campaign(campaign, runner)
    output_json = args.output_json or args.manifest.with_name("campaign-results.json")
    output_markdown = args.output_markdown or args.manifest.with_name("campaign-results.md")
    write_json(output_json, result)
    output_markdown.write_text(render_campaign_report(result), encoding="utf-8")
    print(output_markdown.read_text(encoding="utf-8"))
    if not result["complete"]:
        return 2
    return 1 if result["anomalies"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="write a no-side-effect ten-PR campaign manifest")
    plan.add_argument("--source-repository", required=True)
    plan.add_argument("--source-ref", required=True)
    plan.add_argument("--target-repository", required=True)
    plan.add_argument("--run-id")
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--replace", action="store_true")

    for name in ("prepare", "submit"):
        live = subparsers.add_parser(name)
        live.add_argument("--manifest", type=Path, required=True)
        live.add_argument("--execute", action="store_true")
        live.add_argument("--confirm-target", required=True)
        live.add_argument("--resume", action="store_true")

    collect = subparsers.add_parser("collect", help="collect a read-only campaign snapshot")
    collect.add_argument("--manifest", type=Path, required=True)
    collect.add_argument("--output-json", type=Path)
    collect.add_argument("--output-markdown", type=Path)
    return parser


def main(argv: Sequence[str] | None = None, *, runner: CommandRunner | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command_runner = runner or CommandRunner()
    try:
        if args.command == "plan":
            return plan_command(args)
        if args.command == "prepare":
            return prepare_command(args, command_runner)
        if args.command == "submit":
            return submit_command(args, command_runner)
        if args.command == "collect":
            return collect_command(args, command_runner)
    except CampaignError as exc:
        parser.error(str(exc))
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
