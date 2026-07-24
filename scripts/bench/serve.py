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
from starlette.staticfiles import StaticFiles

PORT: int = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
ROOT: Path = Path(__file__).parent

app = Starlette()
app.mount("/", StaticFiles(directory=ROOT, html=True), name="bench")

if __name__ == "__main__":
    print(f"[OK] serving {ROOT} with Range support on http://localhost:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
