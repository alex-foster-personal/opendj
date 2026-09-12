"""lyrics-align on Modal: MMS-300M CTC forced alignment fanned out per song.

Port of scripts/spike_align_jamendo.py's alignment loop to Modal so a full
79-song corpus round takes ~2 minutes wall instead of ~26 minutes local CPU.
Same prediction contract, same fail-fast rules, same CLI shape; the local
script stays the reference implementation (and the offline fallback).

Requirements
- ✔︎ One container-resident model (modal.enter) aligning many songs; T4, float32
  so results stay comparable to the local float32 CPU reference.
    [if] cuda is unavailable inside the container [then ⛔️] hard error
- ✔︎ Same output contract as the local spike: <pred_dir>/<name>.json with
  {"words": [{word, start_s, end_s, score}]}, aligner word count must equal the
  reference word count.
    [if] aligner returns a different word count [then ⛔️] that song errors
- ✔︎ Per-song isolation: a failed song returns {"error": ...} and the batch
  continues; the driver exits 1 if any song failed.
    [if] any song failed or was skipped-missing [then] exit code 1
- ✔︎ Parity smoke: `parity --against <local_pred_dir>` aligns N songs on Modal
  and compares word onsets to an existing local run.
    [if] fewer than 99% of onsets match within 20 ms [then ⛔️] parity FAIL
- ✔︎ --non-lexical-stars (round 4b, default OFF): vocalization runs detected in the
  reference words are starred out during alignment and their timings linearly
  interpolated from the anchored neighbours; interpolated words carry
  {"interpolated": true, "score": null}. The detector/replacer/interpolator is
  apps/lyrics/nonlexical.py MOUNTED INTO the container -- the exact same file the
  local reference script loads, so the two stay behaviorally identical.
    [if] a run touches a song edge [then] pack it against its single anchor EXPLICITLY (notes)
    [if] a whole song is one run (no anchor either side) [then ⛔️] that song errors

Usage:
  uv run --with modal python scripts/modal_align_spike.py align \
      --audio-dir data/datasets/jamendolyrics-vocals \
      --audio-template "{name}-vocals.mp3,{name}-vocals.flac" \
      --pred-dir data/state/lyrics-eval/round2-modal-parity \
      --star-frequency segment
  uv run --with modal python scripts/modal_align_spike.py parity \
      --audio-dir data/datasets/jamendolyrics-vocals \
      --audio-template "{name}-vocals.mp3,{name}-vocals.flac" \
      --against data/state/lyrics-eval/round2-mms-vocals-starseg \
      --star-frequency segment --limit 3
Then score: uv run --no-sync python -m apps.lyrics score --pred <pred_dir>

-Claude
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

import modal

# ----- config ----------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DATASET_DIR: Path = REPO_ROOT / "data" / "datasets" / "jamendolyrics"

GPU_KIND: str = "T4"  # MMS-300M is small; T4 is the cheapest cuda tier and plenty
TASK_TIMEOUT_S: int = 600
# R6 wall target (specs/karaoke-lyrics-operational-plan.md section 12): ~100
# stem-ready tracks in <= 5 min end-to-end, so the starmap fan-out below must
# be allowed enough containers that alignment is never a serial job loop.
MAX_CONTAINERS: int = 20
SAMPLE_RATE: int = 16_000
STAR_TOKEN: str = "<star>"
PARITY_TOL_S: float = 0.02
PARITY_MIN_MATCH: float = 0.99
LANG_TO_ISO639_3: dict[str, str] = {
    "English": "eng", "German": "deu", "Spanish": "spa", "French": "fra",
}
# Same pin as scripts/spike_align_jamendo.py -- the two MUST move together or
# parity between local and Modal rounds stops meaning anything.
CTC_ALIGNER_PIN: str = (
    "ctc-forced-aligner @ git+https://github.com/MahmoudAshraf97/"
    "ctc-forced-aligner.git@11855d1de76af2b490dd2e8e2db2661805ae90a0"
)
# The one shared non-lexical implementation (round 4b): loaded here by file
# path AND mounted into the container, so local and Modal cannot diverge.
NONLEXICAL_SRC: Path = REPO_ROOT / "apps" / "lyrics" / "nonlexical.py"
NONLEXICAL_REMOTE: str = "/root/nonlexical.py"


def _bake_model() -> None:
    """Download the MMS-300M checkpoint into the image's HF cache at build time
    so runtime containers start hot (build runs on CPU; runtime loads cuda)."""
    from ctc_forced_aligner import load_alignment_model

    load_alignment_model("cpu")
    print("[build] baked MMS-300M aligner checkpoint into the image")


image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "git")  # git: the aligner dep is a git+ pin
    .pip_install("torch>=2.4", "numpy", CTC_ALIGNER_PIN)
    .run_function(_bake_model)
    .add_local_file(NONLEXICAL_SRC, NONLEXICAL_REMOTE)
)

app = modal.App(name="mdt-align-spike", image=image)


# ----- remote GPU class ------------------------------------------------------


@app.cls(gpu=GPU_KIND, timeout=TASK_TIMEOUT_S, max_containers=MAX_CONTAINERS)
class AlignWorker:
    @modal.enter()
    def load(self) -> None:
        import importlib.util

        import torch
        from ctc_forced_aligner import load_alignment_model

        if not torch.cuda.is_available():
            raise RuntimeError("cuda unavailable inside the T4 container")
        # float32, not half: keeps onsets comparable with the local float32
        # CPU reference (the parity subcommand enforces this stays true).
        self.model, self.tokenizer = load_alignment_model("cuda", dtype=torch.float32)
        # Same file the local reference script loads (mounted via add_local_file).
        spec = importlib.util.spec_from_file_location("nonlexical", NONLEXICAL_REMOTE)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load non-lexical module from {NONLEXICAL_REMOTE}")
        self.nonlex = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.nonlex)

    @modal.method()
    def align(
        self,
        name: str,
        audio_bytes: bytes,
        words: list[str],
        language_iso: str,
        star_frequency: str,
        batch_size: int,
        non_lexical_stars: bool,
    ) -> dict[str, Any]:
        """One song. Returns {name, words|error, gpu_s}; never raises, so one
        bad song cannot abort the whole map (same isolation as the roformer
        spike's separate_track)."""
        import subprocess

        import numpy as np
        import torch
        from ctc_forced_aligner import (
            generate_emissions,
            get_alignments,
            get_spans,
            postprocess_results,
            preprocess_text,
        )

        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", "pipe:0",
                 "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
                input=audio_bytes, capture_output=True, check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode()[:500]}")
            audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
            if audio.size < SAMPLE_RATE:
                raise RuntimeError(f"decoded <1s of audio ({audio.size} samples)")
            waveform = torch.from_numpy(audio).to(self.model.dtype).to(self.model.device)
            emissions, stride = generate_emissions(self.model, waveform, batch_size=batch_size)
            tokens_starred, text_starred = preprocess_text(
                " ".join(words), romanize=True, language=language_iso,
                star_frequency=star_frequency,
            )
            runs = self.nonlex.detect_nonlexical_runs(words) if non_lexical_stars else []
            if runs:
                tokens_starred, text_starred = self.nonlex.replace_run_tokens_with_stars(
                    tokens_starred, text_starred, len(words), star_frequency, runs
                )
            segments, scores, blank_token = get_alignments(emissions, tokens_starred, self.tokenizer)
            spans = get_spans(tokens_starred, segments, blank_token)
            stamps = postprocess_results(text_starred, spans, stride, scores)
            stamps = [s for s in stamps if s["text"] != STAR_TOKEN]
            notes: list[str] = []
            if runs:
                out_words, notes = self.nonlex.interpolate_run_words(
                    words, stamps, runs, audio.size / SAMPLE_RATE
                )
                notes.append(f"{len(runs)} run(s), "
                             f"{sum(len(r) for r in runs)} words starred + interpolated")
            else:
                if len(stamps) != len(words):
                    raise RuntimeError(
                        f"aligner returned {len(stamps)} words for {len(words)} reference words"
                    )
                out_words = [
                    {"word": w, "start_s": s["start"], "end_s": s["end"], "score": s["score"]}
                    for w, s in zip(words, stamps, strict=True)
                ]
        except Exception as exc:  # noqa: BLE001 -- per-song isolation is the whole point
            return {"name": name, "error": f"{type(exc).__name__}: {exc}",
                    "gpu_s": time.perf_counter() - t0}
        return {
            "name": name,
            "error": None,
            "gpu_s": time.perf_counter() - t0,
            "notes": notes,
            "words": out_words,
        }


# ----- local driver ----------------------------------------------------------


def _load_dataset_index() -> list[dict[str, str]]:
    index_csv = DATASET_DIR / "JamendoLyrics.csv"
    if not index_csv.is_file():
        raise FileNotFoundError(f"{index_csv} missing -- run `uv run scripts/pull_jamendolyrics.py`")
    with index_csv.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_words(name: str) -> list[str]:
    raw = (DATASET_DIR / "lyrics" / f"{name}.words.txt").read_text(encoding="utf-8")
    return [w for w in raw.split("\n") if w.strip()]


def _resolve_audio_path(
    audio_dir: Path, audio_template: str, name: str, *, missing_ok: bool
) -> Path | None:
    """Mirror of the local spike's resolver: exactly one candidate must exist;
    >1 is always fatal, 0 is fatal unless missing_ok (returns None to skip)."""
    candidates = [audio_dir / t.strip().format(name=name) for t in audio_template.split(",")]
    existing = [p for p in candidates if p.resolve().is_file()]
    if len(existing) > 1:
        raise SystemExit(
            f"[ERROR] {name}: {len(existing)} audio candidates exist "
            f"({', '.join(str(p) for p in existing)}) -- ambiguous, need exactly 1"
        )
    elif not existing:
        if missing_ok:
            return None
        raise SystemExit(
            f"[ERROR] {name}: no audio candidate exists "
            f"({', '.join(str(p) for p in candidates)}) -- wrong --audio-dir/--audio-template?"
        )
    return existing[0]


def _select_rows(args: argparse.Namespace) -> list[dict[str, str]]:
    rows = _load_dataset_index()
    if args.languages:
        wanted = set(args.languages.split(","))
        unknown = wanted - set(LANG_TO_ISO639_3)
        if unknown:
            raise SystemExit(f"unknown languages {sorted(unknown)}; valid: {sorted(LANG_TO_ISO639_3)}")
        rows = [r for r in rows if r["Language"] in wanted]
    if args.song:
        rows = [r for r in rows if Path(r["Filepath"]).stem == args.song]
        if not rows:
            raise SystemExit(f"song {args.song!r} not in dataset index")
    if args.limit:
        rows = rows[: args.limit]
    return rows


AlignJob = tuple[str, bytes, list[str], str, str, int, bool]


def _build_jobs(
    rows: list[dict[str, str]], args: argparse.Namespace, *, skip_existing: bool
) -> tuple[list[AlignJob], int, int]:
    """Returns (jobs, n_skipped_existing, n_missing_audio); loud per skip."""
    jobs: list[AlignJob] = []
    skipped = missing = 0
    for row in rows:
        name = Path(row["Filepath"]).stem
        if skip_existing and (args.pred_dir / f"{name}.json").exists() and not args.force:
            skipped += 1
            print(f"[SKIP] {name} (exists)")
            continue
        audio_path = _resolve_audio_path(
            args.audio_dir, args.audio_template, name, missing_ok=args.skip_missing_audio
        )
        if audio_path is None:
            missing += 1
            print(f"[SKIP-MISSING] {name} (no audio yet)")
            continue
        jobs.append((
            name,
            audio_path.resolve().read_bytes(),
            _load_words(name),
            LANG_TO_ISO639_3[row["Language"]],
            args.star_frequency,
            args.batch_size,
            args.non_lexical_stars,
        ))
    return jobs, skipped, missing


def _print_notes(result: dict[str, Any]) -> None:
    for note in result.get("notes", []):
        print(f"[NONLEX] {result['name']}: {note}")


def cmd_align(args: argparse.Namespace) -> int:
    rows = _select_rows(args)
    args.pred_dir.mkdir(parents=True, exist_ok=True)
    jobs, _, missing = _build_jobs(rows, args, skip_existing=True)
    if not jobs:
        print("[DONE] nothing to align")
        return 1 if missing else 0

    done = failed = 0
    t_run = time.monotonic()
    with app.run():
        worker = AlignWorker()
        for result in worker.align.starmap(jobs, order_outputs=False):
            if result["error"] is not None:
                failed += 1
                print(f"[ERROR] {result['name']}: {result['error']}", file=sys.stderr)
                continue
            _print_notes(result)
            out_path = args.pred_dir / f"{result['name']}.json"
            out_path.write_text(
                json.dumps({"words": result["words"]}, ensure_ascii=False), encoding="utf-8"
            )
            done += 1
            print(f"[OK] {done + failed}/{len(jobs)} {result['name']}: "
                  f"{len(result['words'])} words in {result['gpu_s']:.1f}s gpu")
    print(f"[DONE] aligned {done}, failed {failed}, missing-audio {missing}, "
          f"wall {time.monotonic() - t_run:.0f}s -> {args.pred_dir}")
    return 1 if (failed or missing) else 0


def cmd_parity(args: argparse.Namespace) -> int:
    """Align N songs on Modal and compare onsets against an existing local run."""
    against: Path = args.against
    rows = [r for r in _select_rows(args)
            if (against / f"{Path(r['Filepath']).stem}.json").is_file()]
    if not rows:
        raise SystemExit(f"[ERROR] no songs in {against} overlap the selection")
    args.pred_dir = args.against  # unused for writing; keeps _build_jobs happy
    args.force = True
    jobs, _, missing = _build_jobs(rows, args, skip_existing=False)
    if missing:
        raise SystemExit("[ERROR] parity selection must have audio for every song")

    total = matched = 0
    with app.run():
        worker = AlignWorker()
        for result in worker.align.starmap(jobs, order_outputs=False):
            if result["error"] is not None:
                raise SystemExit(f"[ERROR] {result['name']}: {result['error']}")
            _print_notes(result)
            local = json.loads((against / f"{result['name']}.json").read_text(encoding="utf-8"))
            local_starts = [w["start_s"] for w in local["words"]]
            modal_starts = [w["start_s"] for w in result["words"]]
            if len(local_starts) != len(modal_starts):
                raise SystemExit(f"[ERROR] {result['name']}: word count mismatch vs local run")
            pairs = list(zip(local_starts, modal_starts, strict=True))
            song_matched = sum(abs(a - b) <= PARITY_TOL_S for a, b in pairs)
            total += len(pairs)
            matched += song_matched
            print(f"[..] {result['name']}: {song_matched}/{len(pairs)} onsets within "
                  f"{PARITY_TOL_S * 1000:.0f}ms of local")
    rate = matched / total
    verdict = "PASS" if rate >= PARITY_MIN_MATCH else "FAIL"
    print(f"[{verdict}] parity {matched}/{total} = {rate:.2%} "
          f"(threshold {PARITY_MIN_MATCH:.0%} within {PARITY_TOL_S * 1000:.0f}ms)")
    return 0 if verdict == "PASS" else 1


def cmd_align_files(args: argparse.Namespace) -> int:
    """Align arbitrary tracks from a jobs manifest (own-crate round 3b path).

    Manifest: JSON list of {name, stem_audio_template: [candidate paths],
    words: [...], language_iso}; exactly ONE stem candidate must exist per job
    (missing is skipped loudly with --skip-missing-audio, ambiguous is fatal).
    """
    manifest = json.loads(args.jobs.read_text(encoding="utf-8"))
    if not manifest:
        raise SystemExit(f"[ERROR] {args.jobs} holds zero jobs")
    args.pred_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[AlignJob] = []
    missing = 0
    for entry in manifest:
        name = entry["name"]
        if (args.pred_dir / f"{name}.json").exists() and not args.force:
            print(f"[SKIP] {name} (exists)")
            continue
        candidates = [Path(p) for p in entry["stem_audio_template"]]
        existing = [p for p in candidates if p.is_file()]
        if len(existing) > 1:
            raise SystemExit(f"[ERROR] {name}: {len(existing)} stem candidates exist -- ambiguous")
        elif not existing:
            if not args.skip_missing_audio:
                raise SystemExit(
                    f"[ERROR] {name}: no stem candidate exists "
                    f"({', '.join(str(p) for p in candidates)}) -- separate first?"
                )
            missing += 1
            print(f"[SKIP-MISSING] {name} (no stem yet)")
            continue
        jobs.append((
            name, existing[0].read_bytes(), entry["words"], entry["language_iso"],
            args.star_frequency, args.batch_size, args.non_lexical_stars,
        ))
    if not jobs:
        print("[DONE] nothing to align")
        return 1 if missing else 0

    done = failed = 0
    t_run = time.monotonic()
    with app.run():
        worker = AlignWorker()
        for result in worker.align.starmap(jobs, order_outputs=False):
            if result["error"] is not None:
                failed += 1
                print(f"[ERROR] {result['name']}: {result['error']}", file=sys.stderr)
                continue
            _print_notes(result)
            (args.pred_dir / f"{result['name']}.json").write_text(
                json.dumps({"words": result["words"]}, ensure_ascii=False), encoding="utf-8"
            )
            done += 1
            print(f"[OK] {done + failed}/{len(jobs)} {result['name']}: "
                  f"{len(result['words'])} words in {result['gpu_s']:.1f}s gpu")
    print(f"[DONE] aligned {done}, failed {failed}, missing-stem {missing}, "
          f"wall {time.monotonic() - t_run:.0f}s -> {args.pred_dir}")
    return 1 if (failed or missing) else 0


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audio-dir", type=Path, default=DATASET_DIR / "mp3")
    parser.add_argument("--audio-template", type=str, default="{name}.mp3",
                        help="comma-separated candidates, exactly ONE must exist per song")
    parser.add_argument("--languages", type=str, default=None)
    parser.add_argument("--song", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--star-frequency", choices=("edges", "segment"), default="segment",
                        help="segment (round-2 default) unless reproducing rounds 0-1")
    parser.add_argument("--non-lexical-stars", action="store_true",
                        help="round 4b: star out detected vocalization runs "
                             "(apps/lyrics/nonlexical.py) and interpolate their "
                             "timings from the anchored neighbour words")
    parser.add_argument("--skip-missing-audio", action="store_true")
    parser.add_argument("--force", action="store_true")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    align = sub.add_parser("align", help="align songs on Modal, write prediction JSONs")
    align.add_argument("--pred-dir", type=Path, required=True)
    _add_common_args(align)
    align.set_defaults(func=cmd_align)

    parity = sub.add_parser("parity", help="compare a Modal run against a local pred dir")
    parity.add_argument("--against", type=Path, required=True,
                        help="local prediction dir to compare onsets against")
    _add_common_args(parity)
    parity.set_defaults(func=cmd_parity)

    align_files = sub.add_parser(
        "align-files", help="align arbitrary tracks from a jobs manifest JSON"
    )
    align_files.add_argument("--jobs", type=Path, required=True)
    align_files.add_argument("--pred-dir", type=Path, required=True)
    align_files.add_argument("--batch-size", type=int, default=4)
    align_files.add_argument("--star-frequency", choices=("edges", "segment"), default="segment")
    align_files.add_argument("--non-lexical-stars", action="store_true")
    align_files.add_argument("--skip-missing-audio", action="store_true")
    align_files.add_argument("--force", action="store_true")
    align_files.set_defaults(func=cmd_align_files)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
