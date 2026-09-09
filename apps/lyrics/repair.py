"""Stage-4 repair: apply an arbitrated verdict's findings as edits to the sheet.

specs/lyrics-version-check.md round 3. Repair NEVER acts on its own judgement:
it edits only the decisive findings of an ARBITRATED verdict, and the
acceptance gate is closing the loop -- the repaired sheet must re-enter the
full pipeline (classify + arbitrate) and come back `matched` ("green after
repair", the maintainer's bar). unverifiable and wrong_song verdicts are refused here:
those sheets route to review or re-sourcing, not editing.

Edits by finding kind (sheet-centric):
- sheet_repeat_unsupported / missing_in_audio (arbitrated REAL): DELETE the
  span -- the sheet claims words this version does not sing.
- moved_block: MOVE the span to where the audio actually sings it (source
  identity kept -- the words are the same words).
- audio_repeat_unlisted: INSERT a copy of the matching sheet block at the
  position difflib anchors the extra sung repeat (copy carries NO source --
  its twin keeps the original identity).
- extra_in_audio (non-repeat): INSERT the ASR words themselves, flagged
  asr_sourced (casing/punctuation are ASR's, not a lyric source's).

Every repaired word carries provenance: source index into the INPUT sheet, or
None for inserted material -- so end-to-end scoring keeps honest denominators.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from apps.lyrics.asr_match import normalize_word
from apps.lyrics.verdict import REPEAT_SIM, Verdict

#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairResult:
    words: list[str]
    source_idx: list[int | None]  # per repaired word: input-sheet index, None = inserted
    edits: list[str]  # human-readable log, one entry per applied edit
    asr_sourced_spans: int  # inserts whose text came from ASR rather than the sheet


def repair_sheet(sheet_words: list[str], asr_words: list[str], verdict: Verdict) -> RepairResult:
    if verdict.verdict in ("unverifiable", "wrong_song"):
        raise ValueError(
            f"refusing to repair a {verdict.verdict} sheet -- route to review/re-sourcing"
        )
    if not verdict.findings:
        raise ValueError("nothing to repair: verdict carries no findings")

    sheet_norm = [normalize_word(w) for w in sheet_words]
    asr_norm = [normalize_word(w) for w in asr_words]
    opcodes = SequenceMatcher(a=sheet_norm, b=asr_norm, autojunk=False).get_opcodes()

    deletions, inserts, edits, asr_sourced = _plan_edits(
        verdict, sheet_norm, asr_words, opcodes
    )

    words: list[str] = []
    source_idx: list[int | None] = []

    for i in range(len(sheet_words) + 1):
        for at, block, label in inserts:
            if at == i:
                _emit_block(block, label, sheet_words, words, source_idx, edits)
        if i < len(sheet_words) and i not in deletions:
            words.append(sheet_words[i])
            source_idx.append(i)
    if not words:
        raise ValueError("repair deleted the entire sheet -- refusing")
    return RepairResult(
        words=words, source_idx=source_idx, edits=edits, asr_sourced_spans=asr_sourced
    )


#-----------------------------------------------------------------------------


def _emit_block(
    block: list[tuple],
    label: str,
    sheet_words: list[str],
    words: list[str],
    source_idx: list[int | None],
    edits: list[str],
) -> None:
    """Append one planned block to the repaired sheet, keeping source identity.

    ``keep`` carries its input index through (a moved word is the SAME word),
    ``dup`` and ``asr`` claim no source, so a later reader can tell repaired
    text from sourced text.
    """
    for entry in block:
        if entry[0] == "keep":
            words.append(sheet_words[entry[1]])
            source_idx.append(entry[1])
        elif entry[0] == "dup":
            words.append(sheet_words[entry[1]])
            source_idx.append(None)
        else:
            for w in entry[1]:
                words.append(w)
                source_idx.append(None)
    edits.append(label)


def _plan_edits(
    verdict: Verdict, sheet_norm: list[str], asr_words: list[str], opcodes: list,
) -> tuple[set[int], list[tuple[int, list[tuple], str]], list[str], int]:
    """Turn findings into the edit plan: what to delete, what to insert where.

    Block entries: ``("keep", idx)`` preserves source identity (moved words),
    ``("dup", idx)`` is a copy with no source claim, ``("asr", [words])`` is
    ASR text. Nothing is applied here - the caller replays the plan in sheet
    order so an insert position means the same thing for every finding kind.
    """
    deletions: set[int] = set()
    inserts: list[tuple[int, list[tuple], str]] = []  # (sheet position, block, log label)
    edits: list[str] = []
    asr_sourced = 0
    for f in verdict.findings:
        if f.kind in ("sheet_repeat_unsupported", "missing_in_audio"):
            deletions.update(range(f.sheet_start, f.sheet_end + 1))
            edits.append(f"delete sheet words {f.sheet_start}-{f.sheet_end} ({f.kind})")
        elif f.kind == "moved_block":
            deletions.update(range(f.sheet_start, f.sheet_end + 1))
            at = _sheet_position_of_asr_block(opcodes, f.asr_start)
            block = [("keep", i) for i in range(f.sheet_start, f.sheet_end + 1)]
            inserts.append(
                (at, block, f"move sheet words {f.sheet_start}-{f.sheet_end} to pos {at}")
            )
        elif f.kind == "audio_repeat_unlisted":
            twin = _best_sheet_window(f.text, sheet_norm)
            if twin is None:
                raise ValueError(
                    f"audio_repeat_unlisted finding has no matching sheet window: {f.text[:60]}"
                )
            at = _sheet_position_of_asr_block(opcodes, f.asr_start)
            block = [("dup", i) for i in range(twin[0], twin[1] + 1)]
            inserts.append(
                (at, block, f"insert chorus copy (sheet {twin[0]}-{twin[1]}) at pos {at}")
            )
        elif f.kind == "extra_in_audio":
            at = _sheet_position_of_asr_block(opcodes, f.asr_start)
            block = [("asr", [asr_words[i] for i in range(f.asr_start, f.asr_end + 1)])]
            inserts.append((at, block, f"insert {f.n_words} ASR-sourced words at pos {at}"))
            asr_sourced += 1
        else:
            raise ValueError(f"unhandled finding kind {f.kind!r}")
    return deletions, inserts, edits, asr_sourced


def _sheet_position_of_asr_block(opcodes: list, asr_start: int) -> int:
    """The sheet index where an ASR-side block anchors: the opcode covering
    asr_start gives its sheet-side position directly."""
    for _tag, i1, _i2, j1, j2 in opcodes:
        if j1 <= asr_start < max(j2, j1 + 1):
            return i1
    return opcodes[-1][2]  # past the end: append


def _best_sheet_window(text: str, sheet_norm: list[str]) -> tuple[int, int] | None:
    words = text.split()
    n = len(words)
    best, best_sim = None, REPEAT_SIM
    for start in range(len(sheet_norm) - n + 1):
        sim = SequenceMatcher(
            a=words, b=sheet_norm[start : start + n], autojunk=False
        ).ratio()
        if sim >= best_sim:
            best, best_sim = (start, start + n - 1), sim
    return best
