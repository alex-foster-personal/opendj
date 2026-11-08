"""A real screenshot probe process for act.py's stall gate tests (issue #3369).

Usage: screenshot_probe.py OUT_DIR FRAME [FRAME ...]

Listens on 127.0.0.1, writes OUT_DIR/probe.port once it accepts connections, and answers
each JSON request line by writing the next FRAME (the last one repeats) to
OUT_DIR/screenshots/NNNN.png, so act.py hashes real screenshot bytes on disk.
"""

from __future__ import annotations

import json
import signal
import socket
import sys
from pathlib import Path


def _read_request(conn: socket.socket) -> dict[str, object] | None:
    data = b""
    while b"\n" not in data:
        piece = conn.recv(65536)
        if not piece:
            break
        data += piece
    if not data.strip():
        return None
    return json.loads(data.splitlines()[0])


def serve(out_dir: Path, frames: list[bytes]) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probe.port").write_text(f"{server.getsockname()[1]}\n", encoding="utf-8")
    counter = 0
    while True:
        conn, _ = server.accept()
        with conn:
            request = _read_request(conn)
            if request is None:
                continue
            counter += 1
            rel = f"screenshots/{counter:04d}.png"
            (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (out_dir / rel).write_bytes(frames[min(counter, len(frames)) - 1])
            reply = {
                "id": request.get("id"),
                "action": request.get("action"),
                "target": request.get("target"),
                "outcome": "ok",
                "error": None,
                "screenshot": rel,
                "page": None,
            }
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("usage: screenshot_probe.py OUT_DIR FRAME [FRAME ...]")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    serve(Path(argv[0]), [frame.encode("utf-8") for frame in argv[1:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
