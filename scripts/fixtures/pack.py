"""Versioned fixture packs: fully analyzed tracks for cloud sessions, without the faff.

A pack is one zip made by the perf-lab ``fixture.py export`` (state.db and
master.plain.db copies, the bundled tracks' audio, their anlz-cache, waveform,
stem and lyric files, manifest.json). Packs live as assets on GitHub releases
tagged ``fixture-pack-*`` in the PRIVATE repo private_owner/odj-private,
next to the real-library tarballs already kept there. The audio and the whole
library's metadata are in every pack, so a pack never goes in a public repo,
an artifact, or an issue.

publish (Silver, or any machine with the real library and gh signed in):
  python scripts/fixtures/pack.py publish --data-dir <repo>/data --pick 6
  python scripts/fixtures/pack.py publish --data-dir <repo>/data --ids sid1,sid2,...
  python scripts/fixtures/pack.py publish --zip odj-fixture.zip   # a zip already exported
  Each publish is a new immutable version (tag fixture-pack-YYYYMMDD-HHMMSS unless
  --tag). The importer (fixture.py) is uploaded with the zip, so any later
  session can unpack the pack even on a branch that lacks the script.

ensure (any session; idempotent, cheap when already done):
  python scripts/fixtures/pack.py ensure            # newest pack
  python scripts/fixtures/pack.py ensure --tag fixture-pack-20261013-073000
  Finds the pack in a local cache, then the project's shared folder
  (/mnt/project-files/fixtures/packs), then GitHub; checks its sha256; unpacks it
  with the pack's own fixture.py into ~/.cache/odj-fixtures/<tag>/data. Prints the
  data dir on the last line (``--print-env`` prints ODJ_FIXTURE_* lines instead),
  so callers can ``DATA=$(python scripts/fixtures/pack.py ensure | tail -1)``.

list: the published packs, newest first.

GitHub access goes through ``gh api`` (REST only; cloud sessions refuse GraphQL),
so the session needs odj-private attached. When it is not, ``ensure`` still
works from the shared-folder cache that the first fetching session fills.
"""

from __future__ import annotations

import argparse
import sys

try:
    from scripts.fixtures.pack_common import *  # noqa: F403
    from scripts.fixtures.pack_ensure_flow import cmd_ensure
    from scripts.fixtures.pack_publish import cmd_adopt, cmd_publish
    from scripts.fixtures.pack_serve import cmd_list
    from scripts.fixtures.pack_verify import _location_rows
except ImportError:
    from pack_common import *  # noqa: F403
    from pack_ensure_flow import cmd_ensure
    from pack_publish import cmd_adopt, cmd_publish
    from pack_serve import cmd_list
    from pack_verify import _location_rows


def _arg_text(raw: str) -> str:
    """Command-line text as the UTF-8 the shell passed, whatever the locale.

    Under a non-UTF-8 POSIX locale Python decodes argv bytes with surrogateescape,
    so a note would be stored as lone surrogates rather than its characters.
    """
    return raw.encode(sys.getfilesystemencoding(), "surrogateescape").decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--data-dir")
    src.add_argument("--zip")
    p.add_argument("--ids", default="")
    p.add_argument("--pick", type=int, default=6)
    p.add_argument("--max-audio-mb", type=float, default=400)
    p.add_argument("--with-stems", action="store_true")
    p.add_argument("--tag")
    p.add_argument("--note", default="", type=_arg_text)
    p.add_argument("--fixture-py")
    d = sub.add_parser("adopt")
    d.add_argument("--zip", required=True)
    d.add_argument("--tag")
    d.add_argument("--note", default="", type=_arg_text)
    d.add_argument("--fixture-py")
    d.add_argument(
        "--allow-unanalyzed",
        action="store_true",
        help="adopt a zip whose tracks lack ANLZ files (tests, one-off drops); pack.json lists them",
    )
    e = sub.add_parser("ensure")
    e.add_argument("--tag")
    e.add_argument("--data-dir")
    e.add_argument("--print-env", action="store_true")
    e.add_argument("--force", action="store_true", help="overwrite a --data-dir that holds a library no pack wrote")
    sub.add_parser("list")
    a = ap.parse_args()
    return {"publish": cmd_publish, "adopt": cmd_adopt, "ensure": cmd_ensure, "list": cmd_list}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
