"""lyrics-align round 4a: Whisper ASR of vocal stems on Modal, word timestamps.

The independent witness for the ASR+diff stage: faster-whisper large-v3
transcribes the SEPARATED VOCAL STEM per track and emits word-level timestamps.
Downstream (apps/lyrics/crosscheck.py) diffs the transcript against the lyric
text for version checking, and cross-checks word times against the forced
aligner for "confidently wrong" flags - the two things the aligner's
self-reported score cannot do (Round 3a: per-word self-score is a weak flag;
per-word confidence is MEANINGLESS once the alignment path detaches).

Requirements
- ✔︎ Container-resident large-v3 (modal.enter), T4 float16;
  condition_on_previous_text=False (standard anti-hallucination for music),
  vad_filter off (VAD trims quiet sung onsets; decision recorded here).
    [if] cuda is unavailable in the container [then ⛔️] hard error
- ✔︎ Per-track isolation: a failed track returns {"error": ...}; driver exits 1
  if any track failed.
- ✔︎ Output contract: <out_dir>/<name>.json =
  {"language", "language_probability", "duration_s", "text",
   "words": [{word, start_s, end_s, prob}]}.
    [if] whisper returns zero words [then] that is recorded as-is (a mostly
    instrumental stem is a valid result, not an error)
- ✔︎ Known language is PASSED (Jamendo: per-dataset map); unknown language
  (own-crate) lets whisper detect and records its choice + probability.

Usage:
  uv run --with modal python scripts/modal_asr_spike.py transcribe \
      --audio-dir data/datasets/jamendolyrics-vocals \
      --audio-template "{name}-vocals.mp3,{name}-vocals.flac" \
      --out-dir data/state/lyrics-eval/asr-r4a-jamendo
  uv run --with modal python scripts/modal_asr_spike.py transcribe-files \
      --jobs data/state/lyrics-eval/own-crate/jobs.json \
      --stem-key stem_audio_template \
      --out-dir data/state/lyrics-eval/own-crate/asr-r4a

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

GPU_KIND: str = "T4"
TASK_TIMEOUT_S: int = 900
# R6 wall target (specs/karaoke-lyrics-operational-plan.md section 12): ~100
# stem-ready tracks in <= 5 min end-to-end, so the starmap fan-out below must
# be allowed enough containers that ASR is never a serial job loop.
MAX_CONTAINERS: int = 20
WHISPER_MODEL: str = "large-v3"
LANG_TO_WHISPER: dict[str, str] = {
    "English": "en", "German": "de", "Spanish": "es", "French": "fr",
}
# ISO 639-3 (own-crate jobs, round 4c detection) -> whisper language code.
# Deliberately partial: a code absent here (e.g. kik, which whisper does not
# model) falls back to whisper AUTO-DETECT, loudly per track, never silently.
ISO3_TO_WHISPER: dict[str, str] = {
    "eng": "en", "deu": "de", "spa": "es", "fra": "fr", "ita": "it", "por": "pt",
    "nld": "nl", "swe": "sv", "nob": "no", "pol": "pl", "eus": "eu", "cat": "ca",
    # batch-2 (Tue 1 Sep 2026): languages lingua decided on that round's texts,
    # all modeled by whisper large-v3. The un-pinned ukr track reproduced the
    # oltf hallucination class (Croatian 'thanks for following the channel').
    "ukr": "uk", "est": "et", "tgl": "tl", "swa": "sw",
}


def _bake_model() -> None:
    """Download large-v3 into the image cache at build time (CPU build container)."""
    from faster_whisper import WhisperModel

    WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    print(f"[build] baked faster-whisper {WHISPER_MODEL} into the image")


image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    # CTranslate2 does not bundle CUDA runtime libs (torch would); the nvidia
    # wheels + LD_LIBRARY_PATH is the standard fix for libcublas.so.12 errors.
    .pip_install("faster-whisper>=1.1", "nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*")
    .env({
        "LD_LIBRARY_PATH": (
            "/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:"
            "/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib"
        )
    })
    .run_function(_bake_model)
)

app = modal.App(name="mdt-asr-spike", image=image)


# ----- remote GPU class ------------------------------------------------------


@app.cls(gpu=GPU_KIND, timeout=TASK_TIMEOUT_S, max_containers=MAX_CONTAINERS)
class AsrWorker:
    @modal.enter()
    def load(self) -> None:
        from faster_whisper import WhisperModel

        self.model = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")

    @modal.method()
    def transcribe(
        self, name: str, audio_bytes: bytes, language: str | None
    ) -> dict[str, Any]:
        """One track. Returns {name, ...contract} or {name, error}; never raises."""
        import tempfile

        t0 = time.perf_counter()
        try:
            # faster-whisper reads files (ffmpeg/PyAV inside); a temp file is
            # simpler and more robust than decoding to PCM here ourselves.
            with tempfile.NamedTemporaryFile(suffix=Path(name).suffix or ".audio") as tmp:
                tmp.write(audio_bytes)
                tmp.flush()
                segments, info = self.model.transcribe(
                    tmp.name,
                    language=language,
                    word_timestamps=True,
                    condition_on_previous_text=False,
                    vad_filter=False,
                    beam_size=5,
                )
                words: list[dict[str, Any]] = []
                text_parts: list[str] = []
                for seg in segments:
                    text_parts.append(seg.text)
                    for w in seg.words or []:
                        words.append({
                            "word": w.word.strip(),
                            "start_s": w.start,
                            "end_s": w.end,
                            "prob": w.probability,
                        })
        except Exception as exc:  # noqa: BLE001 -- per-track isolation is the whole point
            return {"name": name, "error": f"{type(exc).__name__}: {exc}",
                    "gpu_s": time.perf_counter() - t0}
        return {
            "name": name,
            "error": None,
            "gpu_s": time.perf_counter() - t0,
            "language": info.language,
            "language_probability": info.language_probability,
            "duration_s": info.duration,
            "text": "".join(text_parts).strip(),
            "words": words,
        }


# ----- local driver ----------------------------------------------------------


def _resolve_audio(audio_dir: Path, template: str, name: str) -> Path:
    candidates = [audio_dir / t.strip().format(name=name) for t in template.split(",")]
    existing = [p for p in candidates if p.resolve().is_file()]
    if len(existing) != 1:
        raise SystemExit(
            f"[ERROR] {name}: {len(existing)} audio candidates exist "
            f"({', '.join(str(p) for p in candidates)}) -- need exactly 1"
        )
    return existing[0]


def _run_jobs(
    jobs: list[tuple[str, bytes, str | None]], out_dir: Path, suffix_names: dict[str, str]
) -> int:
    done = failed = 0
    t_run = time.monotonic()
    with app.run():
        worker = AsrWorker()
        for result in worker.transcribe.starmap(jobs, order_outputs=False):
            name = result["name"]
            out_name = suffix_names[name]
            if result["error"] is not None:
                failed += 1
                print(f"[ERROR] {out_name}: {result['error']}", file=sys.stderr)
                continue
            payload = {k: result[k] for k in
                       ("language", "language_probability", "duration_s", "text", "words")}
            (out_dir / f"{out_name}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            done += 1
            print(f"[OK] {done + failed}/{len(jobs)} {out_name}: {len(result['words'])} "
                  f"asr words ({result['language']} p={result['language_probability']:.2f}) "
                  f"in {result['gpu_s']:.1f}s gpu")
    print(f"[DONE] transcribed {done}, failed {failed}, "
          f"wall {time.monotonic() - t_run:.0f}s -> {out_dir}")
    return 1 if failed else 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    index_csv = DATASET_DIR / "JamendoLyrics.csv"
    if not index_csv.is_file():
        raise SystemExit(f"{index_csv} missing -- run `uv run scripts/pull_jamendolyrics.py`")
    with index_csv.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if args.languages:
        wanted = set(args.languages.split(","))
        rows = [r for r in rows if r["Language"] in wanted]
    if args.song:
        rows = [r for r in rows if Path(r["Filepath"]).stem == args.song]
        if not rows:
            raise SystemExit(f"song {args.song!r} not in dataset index")
    if args.limit:
        rows = rows[: args.limit]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, bytes, str | None]] = []
    suffix_names: dict[str, str] = {}
    for row in rows:
        name = Path(row["Filepath"]).stem
        if (args.out_dir / f"{name}.json").exists() and not args.force:
            print(f"[SKIP] {name} (exists)")
            continue
        audio = _resolve_audio(args.audio_dir, args.audio_template, name)
        job_name = name + audio.suffix  # carries the container ext for the temp file
        jobs.append((job_name, audio.resolve().read_bytes(), LANG_TO_WHISPER[row["Language"]]))
        suffix_names[job_name] = name
    if not jobs:
        print("[DONE] nothing to transcribe")
        return 0
    return _run_jobs(jobs, args.out_dir, suffix_names)


def cmd_transcribe_files(args: argparse.Namespace) -> int:
    manifest = json.loads(args.jobs.read_text(encoding="utf-8"))
    if not manifest:
        raise SystemExit(f"[ERROR] {args.jobs} holds zero jobs")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, bytes, str | None]] = []
    suffix_names: dict[str, str] = {}
    for entry in manifest:
        name = entry["name"]
        if (args.out_dir / f"{name}.json").exists() and not args.force:
            print(f"[SKIP] {name} (exists)")
            continue
        candidates = [Path(p) for p in entry[args.stem_key]]
        existing = [p for p in candidates if p.is_file()]
        if len(existing) != 1:
            raise SystemExit(f"[ERROR] {name}: {len(existing)} stem candidates -- need exactly 1")
        job_name = name + existing[0].suffix
        # Pin whisper's language from the job's (round-4c text-detected) code
        # when whisper models it; otherwise AUTO-DETECT, loudly. NOTE: this
        # pins the LYRIC TEXT's language - when the lyric source itself is
        # wrong (Anchor Point's Basque prose), the pin inherits that error and
        # the cross-check verdict stays unverifiable either way.
        whisper_lang = ISO3_TO_WHISPER.get(entry["language_iso"])
        if whisper_lang is None:
            print(f"[AUTO] {name}: language_iso {entry['language_iso']!r} not in "
                  "ISO3_TO_WHISPER -- whisper will auto-detect")
        jobs.append((job_name, existing[0].read_bytes(), whisper_lang))
        suffix_names[job_name] = name
    if not jobs:
        print("[DONE] nothing to transcribe")
        return 0
    return _run_jobs(jobs, args.out_dir, suffix_names)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    tr = sub.add_parser("transcribe", help="transcribe Jamendo stems (language known per song)")
    tr.add_argument("--audio-dir", type=Path, required=True)
    tr.add_argument("--audio-template", type=str, required=True)
    tr.add_argument("--out-dir", type=Path, required=True)
    tr.add_argument("--languages", type=str, default=None)
    tr.add_argument("--song", type=str, default=None)
    tr.add_argument("--limit", type=int, default=None)
    tr.add_argument("--force", action="store_true")
    tr.set_defaults(func=cmd_transcribe)

    trf = sub.add_parser("transcribe-files", help="transcribe from a jobs manifest (language auto)")
    trf.add_argument("--jobs", type=Path, required=True)
    trf.add_argument("--stem-key", type=str, default="stem_audio_template")
    trf.add_argument("--out-dir", type=Path, required=True)
    trf.add_argument("--force", action="store_true")
    trf.set_defaults(func=cmd_transcribe_files)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
