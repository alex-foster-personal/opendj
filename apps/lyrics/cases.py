"""Deterministic eval-case construction for the lyrics-version workstream.

Single source of truth shared by scripts/eval_lyrics_version.py (scoring) and
scripts/bench/lyrics_version_manifest.py (the review page) so the two can
never disagree about what each case's sheet contains. Seeds are crc32 of the
song name: same inputs, same cases, forever.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

from apps.lyrics.jamendo import JamendoSong
from apps.lyrics.perturb import (
    EditOp,
    find_chorus,
    lines_of,
    perturb_audio_extra_chorus,
    perturb_drop_block,
    perturb_move_block,
    perturb_sheet_extra_chorus,
    perturb_sheet_prepend_hook,
)

#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionCase:
    # none | wrong_song | drop_block | move_block | sheet_extra_chorus |
    # sheet_prepend_hook | audio_extra_chorus
    case: str
    sheet_words: list[str]
    expected_verdict: str
    expect_kind: str | None  # finding kind that counts as op recovery
    target_words: list[str] | None  # the block the finding must resemble
    ops: list[EditOp] = field(default_factory=list)
    wrong_song_name: str | None = None  # which song's sheet was substituted
    # Per sheet word: ORIGINAL word index (true timestamps live there), None for
    # inserted copies / wrong-song words. Round-3 end-to-end scoring chains
    # repaired sheet -> case sheet -> original truth through this map.
    source_idx: list[int | None] = field(default_factory=list)


def build_cases(song: JamendoSong, language_peers: list[JamendoSong]) -> list[VersionCase]:
    """All eval cases for one song. language_peers = every song sharing this
    song's language, in dataset order (used for the wrong_song substitute)."""
    true_words = [w.word for w in song.words]
    finals = [w.line_final for w in song.words]
    seed = zlib.crc32(song.name.encode())
    identity: list[int | None] = list(range(len(true_words)))
    cases: list[VersionCase] = [
        VersionCase("none", true_words, "matched", None, None, source_idx=identity)
    ]

    other = language_peers[(language_peers.index(song) + 1) % len(language_peers)]
    if other.name == song.name:
        raise ValueError(f"{song.language} has a single song; wrong_song case impossible")
    cases.append(VersionCase(
        "wrong_song", [w.word for w in other.words], "wrong_song", None, None,
        wrong_song_name=other.name,
        source_idx=[None] * len(other.words),
    ))

    drop = perturb_drop_block(true_words, finals, seed)
    (op,) = drop.ops
    cases.append(VersionCase(
        "drop_block", drop.words, "structure_mismatch", "extra_in_audio",
        true_words[op.word_start : op.word_end + 1], list(drop.ops),
        source_idx=drop.source_idx,
    ))

    move = perturb_move_block(true_words, finals, seed)
    (mop,) = move.ops
    cases.append(VersionCase(
        "move_block", move.words, "structure_mismatch", "moved_block",
        true_words[mop.word_start : mop.word_end + 1], list(move.ops),
        source_idx=move.source_idx,
    ))

    if find_chorus(lines_of(true_words, finals)) is not None:
        dbl = perturb_sheet_extra_chorus(true_words, finals)
        (cop,) = dbl.ops
        chorus_words = true_words[cop.word_start : cop.word_end + 1]
        cases.append(VersionCase(
            "sheet_extra_chorus", dbl.words, "needs_acoustic_check",
            "sheet_repeat_unsupported", chorus_words, list(dbl.ops),
            source_idx=dbl.source_idx,
        ))
        pre = perturb_sheet_prepend_hook(true_words, finals)
        cases.append(VersionCase(
            "sheet_prepend_hook", pre.words, "needs_acoustic_check",
            "sheet_repeat_unsupported", chorus_words, list(pre.ops),
            source_idx=pre.source_idx,
        ))
        aud = perturb_audio_extra_chorus(true_words, finals)
        (aop,) = aud.ops
        cases.append(VersionCase(
            "audio_extra_chorus", aud.words, "structure_mismatch",
            "audio_repeat_unlisted", true_words[aop.word_start : aop.word_end + 1],
            list(aud.ops),
            source_idx=aud.source_idx,
        ))
    return cases
