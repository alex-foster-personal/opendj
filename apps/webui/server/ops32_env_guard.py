"""OPS-32: the single source of truth for which env-var NAME prefixes count
as a leaked Doppler-only secret.

Split out of ``routes/health.py`` on purpose (round 4, issue #2637/#2638):
the health route imports this rather than defining the prefixes inline, so
this one file is what a reader (or a parity test) checks to answer "what
counts as forbidden here" -- never the route body, which changes for
unrelated reasons.

Mirrored 1:1 by ``FORBIDDEN_ENV_PREFIXES`` in
``.agents/skills/ship-dmg/scripts/ship_dmg.sh``. The two are pinned equal by
``tests/scripts/test_ship_dmg_relaunch_env_scrub.py``'s
``test_forbidden_env_prefixes_bash_and_python_agree``, which parses BOTH
real files rather than trusting a copied value in either direction.
"""
from __future__ import annotations

OPS32_FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "R2_",
    "CF_R2_",
    "TAURI_SIGNING_",
    "DOPPLER_",
)
