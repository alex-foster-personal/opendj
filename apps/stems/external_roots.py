"""Name of the env var that lists this machine's external stem stores.

Lives under ``apps`` (not ``scripts``) because the packaged engine ships
``apps`` only: the stems push-missing route maps errors that quote this name
to structured 4xx/503 responses, so the route module must import it without
``scripts`` on ``sys.path``. ``scripts.stem_inventory`` re-exports it and owns
the fail-fast reader.
"""

from __future__ import annotations

EXTERNAL_ROOTS_ENV: str = "MDT_EXTERNAL_STEM_ROOTS"
