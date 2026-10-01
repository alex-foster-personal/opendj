"""Which grid the own beatgrid lane serves (NATIVE-19).

Split out of `own_beatgrid.py` to keep that module under the file-size ceiling.
The modes are `raw` (the model's peak times), `line` (the fitted, BPM-rounded,
offset-corrected line from `apps.analysis_beatgrid.grid_fit`) and
`const_regions` (the same, with the line chosen by the constant-region recipe in
`apps.analysis_beatgrid.const_regions`). The setting lives in
`apps/analysis/config.yaml` under `own_beatgrid.grid_fit`; `MDT_BEATGRID_GRID_FIT`,
when set, overrides it for one process. A fitted record says which mode made it
in its payload's `grid_fit` block.

-Claude
"""

from __future__ import annotations

import os
from typing import Any

from apps.analysis_beatgrid.grid_fit import GRID_FIT_CONST_REGIONS, GRID_FIT_MODES

from .. import config as _analysis_config

GRID_FIT_ENV = "MDT_BEATGRID_GRID_FIT"

#: The mode served when neither the variable nor the config names one
#: (JIK, Thu 1 Oct 2026: round 5 and round 7 put it ahead of `raw` and `line`
#: on every fixed-tempo row).
DEFAULT_GRID_FIT = GRID_FIT_CONST_REGIONS


def grid_fit_mode(config: dict[str, Any] | None = None) -> str:
    """The grid this lane serves: the env override, else the config, else the default.

    An unknown value raises here, naming where it came from, rather than
    falling back to a mode nobody chose.
    """
    override = os.environ.get(GRID_FIT_ENV, "").strip()
    if override:
        mode, source = override, GRID_FIT_ENV
    else:
        if config is None:
            config = _analysis_config.load_config()
        section = config.get("own_beatgrid") or {}
        mode = str(section.get("grid_fit") or DEFAULT_GRID_FIT).strip()
        source = "own_beatgrid.grid_fit in apps/analysis/config.yaml"
    if mode not in GRID_FIT_MODES:
        raise ValueError(f"{source} must be one of {GRID_FIT_MODES}, got {mode!r}")
    return mode


__all__ = ["DEFAULT_GRID_FIT", "GRID_FIT_ENV", "grid_fit_mode"]
