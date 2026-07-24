# /// script
# requires-python = ">=3.10"
# dependencies = ["starlette", "uvicorn"]
# ///
"""Range-capable static server for the bench rating page.

python -m http.server cannot serve HTTP Range requests, so audio players
refuse to seek. StaticFiles supports Range, which makes clips scrubbable.
Usage: uv run scripts/bench/serve.py [port]
"""
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

PORT: int = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
ROOT: Path = Path(__file__).parent


# -----------------------------------------------------------------------------
# no-store for markup, cache for media
# -----------------------------------------------------------------------------
async def _no_store_html(request: Request, call_next) -> Response:
    """Never let a browser reuse a cached rating page.

    StaticFiles sends etag + last-modified but no Cache-Control, so Chrome
    applies heuristic freshness and can serve a stale page without asking.
    That once had a fixed page still showing the old UI on a hard-to-explain
    reload. Markup is a few hundred KB, so no-store costs nothing; audio clips
    are megabytes each and keep their normal revalidating cache.
    """
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


app = Starlette(middleware=[Middleware(BaseHTTPMiddleware, dispatch=_no_store_html)])
app.mount("/", StaticFiles(directory=ROOT, html=True), name="bench")

if __name__ == "__main__":
    print(f"[OK] serving {ROOT} with Range support on http://localhost:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
