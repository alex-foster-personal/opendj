"""Is this audio file a stem-separation output rather than a full mix? (LIBM-170)

A separation tool (demucs, htdemucs, roformer, the modal stems farm) writes
one file per stem. Those files often carry the source song's title and artist,
so once a folder ingest picks one up it sits in the library next to the real
song, the user loads it by mistake, and it plays an acapella with no grid.

The classifier answers from the PATH alone, so a walk can skip a file without
opening it. It is deliberately narrow: a released track whose title merely says
"Instrumental" or "Acapella" is a real song and is never classified here.

A file is a stem output when either of these holds:

* ``separator-dir``: a directory on its path is a separation tool's output
  directory (``stems``, ``separated``, ``mdt-stems``, ``demucs*``,
  ``htdemucs*``, ``*roformer*``).
* ``stem-dir+suffix``: its parent directory is named after a stem
  (``vocals``, ``drums``, ``bass``, ``other``, ...) AND its filename ends in
  that same stem name, e.g. ``vocals/033 - Artist - Title - vocals.mp3``.

A stem-name suffix with no directory signal (``Song - Instrumental.mp3``) is
NOT classified: that is how released instrumentals are named. Nor is a bare
``Vocals.mp3``: the demucs layout ``separated/htdemucs/<song>/vocals.wav`` is
already caught by its directory, and a lone file of that name may be a song.
"""

from __future__ import annotations

import re
from pathlib import PurePath

#: Stem names the separation tools in use write. ``instrumental`` and
#: ``accompaniment`` are the two-stem split's other half.
STEM_NAMES: frozenset[str] = frozenset(
    {"vocals", "drums", "bass", "other", "instrumental", "accompaniment", "no_vocals"}
)
#: Exact directory names a separation tool writes its outputs into.
SEPARATOR_DIR_NAMES: frozenset[str] = frozenset({"stems", "separated", "mdt-stems"})
#: Directory-name prefixes / infixes of separation tools (``htdemucs_ft``,
#: ``demucs``, ``bs_roformer``, ``mel_band_roformer``).
SEPARATOR_DIR_PATTERN: re.Pattern[str] = re.compile(r"^(ht)?demucs|roformer")
_SUFFIX_SEPARATORS: str = " -_.()[]"


def _filename_ends_with_stem(stem_of_name: str, stem_name: str) -> bool:
    lowered = stem_of_name.lower().rstrip(" )]")
    if not lowered.endswith(stem_name):
        return False
    head = lowered[: -len(stem_name)]
    return head == "" or head[-1] in _SUFFIX_SEPARATORS


def stem_output_signal(path: PurePath | str) -> str | None:
    """The signal that marks ``path`` as a stem output, or ``None`` for a song."""
    pure = PurePath(path)
    dirs = [part.lower() for part in pure.parts[:-1]]
    for directory in dirs:
        if directory in SEPARATOR_DIR_NAMES or SEPARATOR_DIR_PATTERN.search(directory):
            return "separator-dir"
    name_stem = pure.stem.lower()
    parent = dirs[-1] if dirs else ""
    if parent in STEM_NAMES and _filename_ends_with_stem(name_stem, parent):
        return "stem-dir+suffix"
    return None


def is_stem_output(path: PurePath | str) -> bool:
    return stem_output_signal(path) is not None


__all__ = [
    "SEPARATOR_DIR_NAMES",
    "SEPARATOR_DIR_PATTERN",
    "STEM_NAMES",
    "is_stem_output",
    "stem_output_signal",
]
