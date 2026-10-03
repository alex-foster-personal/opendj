"""Whether a real Playwright Chromium can launch here, for the PERFMODE-15 real-browser tests.

A test that needs one skips with a reason starting UNAVAILABLE when it cannot,
never passes. The probe launches and closes headless Chromium for real: an
installed `@playwright/test` package without its browser binary (the CI pytest
lanes) must read as unavailable, and only an actual launch tells the two apart.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import cache
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "apps" / "webui" / "frontend"

_LAUNCH_PROBE = """
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const entry = createRequire(path.join(process.cwd(), 'package.json')).resolve('@playwright/test');
const mod = await import(pathToFileURL(entry).href);
const browser = await (mod.default ?? mod).chromium.launch({ headless: true });
await browser.close();
"""


@cache
def chromium_unavailable_reason() -> str | None:
    """None when headless Chromium launches from the frontend's Playwright, else why not."""
    node = shutil.which("node")
    if node is None:
        return "UNAVAILABLE: node is not on PATH"
    node = str(Path(node).resolve())
    probe = subprocess.run(
        [node, "--input-type=module", "-e", _LAUNCH_PROBE],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if probe.returncode != 0:
        detail = next(
            (line.strip() for line in probe.stderr.splitlines() if "launch" in line), probe.stderr.strip()[-400:]
        )
        return f"UNAVAILABLE: Playwright Chromium does not launch here: {detail}"
    return None
