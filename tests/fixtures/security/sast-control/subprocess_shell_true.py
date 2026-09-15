"""Semgrep positive control for odj-subprocess-shell-true (and the p/python pack). Never imported.

scripts/security/scan_sast.sh scans this directory and fails as UNKNOWN when the
`ruleid` line below produces no finding. Normal scans exclude tests/fixtures/security/.
"""

import subprocess


def run_shell(command: str) -> None:
    # ruleid: odj-subprocess-shell-true
    subprocess.run(command, shell=True, check=True)


def run_argv(argv: list[str]) -> None:
    # ok: odj-subprocess-shell-true
    subprocess.run(argv, check=True)
