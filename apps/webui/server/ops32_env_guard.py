"""OPS-32: the single source of truth for which env-var NAME prefixes count
as a leaked Doppler-only secret.

Split out of ``routes/health.py`` on purpose (round 4, issue #2637/#2638):
the health route imports this rather than defining the prefixes inline, so
this one file is what a reader (or a parity test) checks to answer "what
counts as forbidden here" -- never the route body, which changes for
unrelated reasons.

Mirrored 1:1 by ``FORBIDDEN_ENV_PREFIXES`` in
``(private path)``. The two are pinned equal by
``(private path)``'s
``test_forbidden_env_prefixes_bash_and_python_agree``, which parses BOTH
real files rather than trusting a copied value in either direction.
"""

from __future__ import annotations

import sys

OPS32_FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "R2_",
    "CF_R2_",
    "TAURI_SIGNING_",
    "DOPPLER_",
)

#: The variable every real process on this OS carries, read by the health
#: body's ``process_env_home_present`` positive control. POSIX (and a
#: launchd-started macOS app) always has ``HOME``; Windows does not set
#: ``HOME`` by default, and every real Windows process has ``USERPROFILE``
#: instead, so reading ``HOME`` there reported false for a genuine read.
OPS32_HOME_ENV_VAR: str = "USERPROFILE" if sys.platform == "win32" else "HOME"
