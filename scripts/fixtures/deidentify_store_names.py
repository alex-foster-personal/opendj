"""Rewrite the captured store-name evidence so it carries no private inventory.

Run:  python -m scripts.fixtures.deidentify_store_names <in.tsv> <out.tsv>

WHY THIS EXISTS. `cc-store-names.tsv` is real evidence: every row is a cwd a
real Claude Code session recorded, paired with the real directory that session
was written to. That is exactly what makes it worth having, and it is also a
published inventory of one person's private workspace - project names naming a
birthday, a chat recovery, a financial matter, and a second person's Windows
username. This repository has a going-public plan, and a fixture sanitized later
still sits in the history. Codex found it on #708.

WHAT IS PRESERVED, EXACTLY. Every character of every separator, every case, and
every character CLASS. A letter becomes a letter of the same case, a digit
becomes a digit; `/`, `\\`, `:`, `.`, `-`, `_` and every other punctuation
character is untouched, and so is the length of every segment. The two columns
are rewritten by the SAME mapping, so the correspondence between a cwd and its
store name survives intact and per-row equality is still checkable.

WHAT IS ASSUMED, AND IT IS THE COST. Preserving classes rather than characters
bakes in one claim: Claude Code's store-name encoding does not treat particular
LETTERS or DIGITS specially. Separators, case and punctuation, which is what all
three findings behind this fixture were actually about (`/`, then `\\`, then the
drive-letter colon), are carried through untouched and remain real evidence. If
a future finding turns on a specific letter, this fixture cannot speak to it and
a fresh capture is the answer.

Segments that name no one are kept verbatim, so a reader can still see the shape
of a real path. Everything else is mapped through a KEYED hash whose key is 32
random bytes generated when this runs and never written down.

THE KEY IS THE POINT, and the first version got it wrong. That version used a
fixed rotation and committed it, which is a Caesar cipher published beside its
own shift: anyone with the repository could invert the table and read back the
original usernames and project names exactly, so the fixture was cosmetically
sanitized and materially unchanged. Codex refuted it on #708, one round after
the finding that produced it.

A discarded random key makes the mapping irreversible in fact rather than in
manner of speaking. The cost is that the transform is NOT reproducible: rerun it
and you get different names. That is the right trade. The output is the artifact;
this script is here so the METHOD is auditable, not so the mapping can be
recomputed. The manifest's checksum locks the artifact either way.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import string
import sys
from pathlib import Path

#: Segments that describe the OS or the tool rather than the person. Keeping
#: them readable costs nothing: none of them narrows down a human being.
GENERIC = frozenset({
    "", "Users", "Desktop", "Documents", "Downloads", "Library", "home", "var",
    "private", "tmp", "opt", "usr", "code", "src", "projects", "worktrees",
    "tests", "test", "e2e", "artifacts", "proj", "Music", "Movies", "Pictures",
    ".claude", ".codex", ".config", ".local", "node_modules", "dist", "build",
})

#: 32 random bytes, generated once per run and never persisted. Nothing that
#: reaches disk depends on its value, so there is no artifact anyone can use to
#: invert the mapping - which is the whole difference from the rotation this
#: replaced.
_KEY = secrets.token_bytes(32)


def _keystream(piece: str, length: int) -> bytes:
    """Bytes derived from the piece AND the run key, as many as asked for.

    Keyed on the piece so the same piece maps to the same replacement wherever
    it appears - the two columns must agree, or per-row correspondence breaks -
    and keyed on `_KEY` so the mapping cannot be recomputed by a reader.
    Counter mode because blake2b caps a digest at 64 bytes and a path segment
    can be longer.
    """

    out = bytearray()
    block = 0
    while len(out) < length:
        out += hashlib.blake2b(
            f"{block}:{piece}".encode(), key=_KEY, digest_size=64
        ).digest()
        block += 1
    return bytes(out[:length])


def _protected_for(cwd: str) -> frozenset[str]:
    """The pieces this row must carry through untouched, in BOTH columns.

    The generic segments, plus those same segments with a leading dot removed,
    plus this row's drive letter. The encoding flattens `.claude` to `-claude`
    and `C:\\` to `C--`, so a piece protected in the path arrives in the store
    name stripped of its dot or reduced to a bare letter; ciphering it on one
    side only would break the correspondence per-row equality rests on.
    """

    protected = {segment.lstrip(".") for segment in GENERIC} | set(GENERIC)
    if len(cwd) > 1 and cwd[1] == ":":
        # Both spellings: the path carries `C:`, the store name carries a bare
        # `C` because the colon was flattened with the separator.
        protected.update({cwd[0], cwd[:2]})
    return frozenset(protected)


def _rewrite_piece(piece: str, protected: frozenset[str]) -> str:
    """One alphanumeric run, class for class."""

    if piece in protected:
        return piece
    stream = _keystream(piece, len(piece))
    out = []
    for byte, character in zip(stream, piece, strict=True):
        if character in string.ascii_lowercase:
            out.append(string.ascii_lowercase[byte % 26])
        elif character in string.ascii_uppercase:
            out.append(string.ascii_uppercase[byte % 26])
        elif character in string.digits:
            out.append(string.digits[byte % 10])
        else:
            out.append(character)
    return "".join(out)


#: A maximal run of letters and digits. Everything between two runs is a
#: separator of some kind, and the store-name encoding rewrites separators
#: without touching the runs between them.
_RUN = re.compile(r"[A-Za-z0-9]+")


def deidentify(token: str, protected: frozenset[str]) -> str:
    """Both columns go through this, which is the point.

    The unit is a maximal ALPHANUMERIC RUN, not a dash-delimited piece. A path
    and its store name are the same string at two stages of one encoding, and
    that encoding only ever rewrites the SEPARATORS: `\\`, `/`, `:`, `.` and
    `_` all become `-`, while the runs between them are copied through. So the
    runs are the one decomposition both columns agree on, and a replacement
    keyed on the run therefore lands identically in both.

    Splitting on `-` alone is not enough, and the failure is quiet. A directory
    named `foo_bar-123` is one dash-piece in the path and two in the store
    name, so a piece-keyed cipher rewrites it two different ways and the row's
    correspondence breaks - which is exactly what the first keyed revision did
    to 24 of the 39 Windows rows.
    """

    return _RUN.sub(lambda match: _rewrite_piece(match.group(), protected), token)


def main(argv: list[str]) -> int:
    source, destination = Path(argv[1]), Path(argv[2])
    rows = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        cwd, name, platform = line.split("\t")
        rows.append(
            "\t".join(
                (
                    deidentify(cwd, _protected_for(cwd)),
                    deidentify(name, _protected_for(cwd)),
                    platform,
                )
            )
        )
    destination.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"{len(rows)} rows -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
