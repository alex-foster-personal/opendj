"""Code fingerprints for SPIKE-B2 acceptance legs (issue #274).

Regression one-liners:
  - if a leg claims claimed_at_final_revision but its fingerprint differs from HEAD then broken
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys

from apps.shared.paths import PROJECT_ROOT

CODE_SETS: dict[str, tuple[str, ...]] = {
    "python_live": (
        "apps/parity/vocal.py",
        "apps/shared/disposable_dirs.py",
        "apps/shared/process_groups.py",
        "apps/vocals/cache.py",
        "apps/vocals/cli.py",
        "apps/webui/server/rb_vendor_pkg/anlz.py",
        "scripts/vocal_gcloud_farm.py",
        "scripts/vocal_region_worker.py",
        "scripts/vocal_worker_runner.py",
        "tests/fixtures/vocals/spike-b2-acceptance.json",
        "tests/vocals/_process_tree.py",
        "tests/vocals/spike_b2_fixture.py",
        "tests/vocals/test_live_demucs_acceptance.py",
    ),
    "overlay": (
        "apps/shared/disposable_dirs.py",
        "apps/vocals/cache.py",
        "apps/webui/frontend/tests/e2e/playwright.vocals-demucs-overlay.config.ts",
        "apps/webui/frontend/tests/e2e/support/vocals_demucs_fixture.py",
        "apps/webui/frontend/tests/e2e/vite.vocals-demucs-overlay.config.ts",
        "apps/webui/frontend/tests/e2e/vocals-demucs-overlay-endpoints.ts",
        "apps/webui/frontend/tests/e2e/vocals-demucs-overlay.spec.ts",
        "scripts/vocal_region_worker.py",
        "tests/scripts/test_vocals_demucs_fixture.py",
    ),
}


def _read_path_bytes(path: str, rev: str | None) -> bytes:
    if rev is None:
        full = PROJECT_ROOT / path
        if not full.is_file():
            raise FileNotFoundError(f"missing at HEAD: {path}")
        return full.read_bytes()
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return b"<absent>"
    return proc.stdout


def code_fingerprint(code_set: str, rev: str | None = None) -> str:
    if code_set not in CODE_SETS:
        raise KeyError(f"unknown code_set: {code_set}")
    digest = hashlib.sha256()
    for path in sorted(CODE_SETS[code_set]):
        raw = _read_path_bytes(path, rev)
        line = f"{path}\0{hashlib.sha256(raw).hexdigest()}\n"
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def rev_available(rev: str) -> bool:
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}^{{commit}}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _git_head() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="SPIKE-B2 acceptance code fingerprints")
    parser.add_argument("--rev", help="git revision to fingerprint (default: worktree HEAD)")
    args = parser.parse_args()
    rev: str | None = args.rev
    fingerprints = {name: code_fingerprint(name, rev=rev) for name in sorted(CODE_SETS)}
    out = {"git_head": _git_head() if rev is None else rev, "code_fingerprints": fingerprints}
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
