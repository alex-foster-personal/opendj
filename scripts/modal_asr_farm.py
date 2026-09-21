# /// script
# requires-python = ">=3.11"
# dependencies = ["modal", "boto3>=1.34"]
# ///
"""Batch ASR farm: transcribe R2-hosted vocal stems with faster-whisper on Modal.

Reads the published stem-bundle index from R2, pulls each track's content-addressed
vocals part server-to-server inside a T4 container, transcribes with large-v3
(word timestamps, no VAD, no condition_on_previous_text), and writes results back
to R2 under lyrics-asr/v1/. The Mac only plans, drives starmap, and journals.

Requirements
- plan subcommand downloads indexes/stem-bundle-index.json, resolves exactly one
  vocals.mp3 or vocals.flac per stable_id, HEAD-checks lyrics-asr/v1/{id}.json,
  and prints total/done/todo plus any data-error skips. Exit 0 always.
    [if] a stable_id has zero or more than one vocals.* filename [then] skip it
    with a printed warning, do not crash the batch
    [if] --force is passed [then] existing outputs count as todo
- run subcommand fans out todo tracks via .starmap fed by a generator; each
  container verifies sha256, transcribes, uploads JSON, and returns per-track
  errors without raising. Driver journals to data/state/lyrics-asr-journals/ and
  exits 1 if any track failed.
    [if] cuda is unavailable in the container [then ⛔️] that track returns error
    [if] whisper returns zero words [then] that is a valid success, not an error
- verify subcommand HEADs a random sample of 20 output keys plus one known-absent
  negative control; exits nonzero on any absent sample or a present control.
    [if] any sampled real key is ABSENT [then ⛔️] exit 1
    [if] the negative control key is PRESENT [then ⛔️] exit 1

Usage:
  doppler run --project general --config dev_personal -- \\
    uv run --with modal --with boto3 scripts/modal_asr_farm.py plan
  doppler run --project general --config dev_personal -- \\
    uv run --with modal --with boto3 scripts/modal_asr_farm.py run --limit 10 --max-containers 5
  doppler run --project general --config dev_personal -- \\
    uv run --with modal --with boto3 scripts/modal_asr_farm.py verify

-Cursor
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import modal

# ----- config ----------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent

GPU_KIND: str = "T4"
WHISPER_MODEL: str = "large-v3"
TASK_TIMEOUT_S: int = 900  # same wall budget as modal_asr_spike.py
DEFAULT_MAX_CONTAINERS: int = 20

# Vendored from scripts/modal_vocal_farm.py -- must stay identical.
R2_ENV_KEYS: tuple[str, str, str] = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)
DEFAULT_R2_BUCKET: str = "music-dj-audio"
R2_CONTENT_ADDRESSED_KEY: str = "assets/{sha256[:2]}/{sha256}"
R2_MAX_ATTEMPTS: int = 3

INDEX_KEY: str = "indexes/stem-bundle-index.json"
LYRICS_ASR_PREFIX: str = "lyrics-asr/v1"
SCHEMA_VERSION: int = 1
MODEL_LABEL: str = "faster-whisper-large-v3"

VERIFY_SAMPLE_SIZE: int = 20
VERIFY_NEGATIVE_CONTROL: str = (
    f"{LYRICS_ASR_PREFIX}/__verify-negative-control-does-not-exist__.json"
)

JOURNAL_DIR: Path = REPO_ROOT / "data" / "state" / "lyrics-asr-journals"


# ----- modal image -----------------------------------------------------------


def _bake_model() -> None:
    """Download large-v3 into the image cache at build time (CPU build container)."""
    from faster_whisper import WhisperModel

    WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    print(f"[build] baked faster-whisper {WHISPER_MODEL} into the image")


image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "faster-whisper>=1.1",
        "nvidia-cublas-cu12",
        "nvidia-cudnn-cu12==9.*",
        "boto3>=1.34",
    )
    .env({
        "LD_LIBRARY_PATH": (
            "/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:"
            "/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib"
        )
    })
    .run_function(_bake_model)
)

app = modal.App(name="mdt-asr-farm", image=image)


def _r2_secrets() -> list[modal.Secret]:
    """R2 credentials for the container, read from THIS process's environment."""
    if not all(os.environ.get(key) for key in R2_ENV_KEYS):
        return []
    return [modal.Secret.from_dict({key: os.environ[key] for key in R2_ENV_KEYS})]


# ----- local R2 helpers ------------------------------------------------------


def _require_r2_env() -> None:
    missing = [key for key in R2_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise SystemExit(
            f"error: R2 credentials need {missing} in the environment.\n"
            "  Run under: doppler run --project general --config dev_personal -- \\\n"
            "    uv run --with modal --with boto3 scripts/modal_asr_farm.py ..."
        )


def _r2_client() -> Any:
    """S3 client pointed at R2. Works locally (plan/verify) and in-container (run)."""
    import boto3
    from botocore.config import Config

    missing = [key for key in R2_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise RuntimeError(
            f"R2 credentials absent: {missing}. Start under `doppler run --` so "
            "_r2_secrets() can forward them."
        )
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": R2_MAX_ATTEMPTS, "mode": "standard"}),
    )


def _r2_bucket() -> str:
    return os.environ.get("R2_BUCKET", DEFAULT_R2_BUCKET)


def _content_addressed_key(sha256: str) -> str:
    return f"assets/{sha256[:2]}/{sha256}"


def _output_key(stable_id: str) -> str:
    return f"{LYRICS_ASR_PREFIX}/{stable_id}.json"


def _r2_head_exists(client: Any, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return False
        raise


def _load_index(client: Any, bucket: str) -> dict[str, Any]:
    resp = client.get_object(Bucket=bucket, Key=INDEX_KEY)
    return json.loads(resp["Body"].read())


# ----- index resolution ------------------------------------------------------


@dataclass(frozen=True)
class VocalsTrack:
    stable_id: str
    vocals_filename: str
    vocals_sha256: str


def _resolve_vocals(files: dict[str, str]) -> tuple[str, str] | None:
    matches = [
        (filename, digest)
        for filename, digest in files.items()
        if filename in ("vocals.mp3", "vocals.flac")
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _iter_index_tracks(index: dict[str, Any]) -> Iterator[tuple[str, dict[str, str]]]:
    stable_ids = index.get("stable_ids", {})
    if not isinstance(stable_ids, dict):
        raise SystemExit(f"[ERROR] index missing stable_ids map at {INDEX_KEY}")
    for stable_id, files in stable_ids.items():
        if not isinstance(files, dict):
            raise SystemExit(f"[ERROR] index entry for {stable_id!r} is not a file map")
        yield stable_id, files


def _classify_index(
    client: Any, bucket: str, index: dict[str, Any], force: bool
) -> tuple[list[VocalsTrack], list[str], int, int]:
    """Return (todo, data_error_ids, done_count, total_count)."""
    todo: list[VocalsTrack] = []
    data_errors: list[str] = []
    done = 0
    total = 0
    for stable_id, files in _iter_index_tracks(index):
        total += 1
        resolved = _resolve_vocals(files)
        if resolved is None:
            data_errors.append(stable_id)
            continue
        vocals_filename, vocals_sha256 = resolved
        track = VocalsTrack(stable_id, vocals_filename, vocals_sha256)
        if _r2_head_exists(client, bucket, _output_key(stable_id)) and not force:
            done += 1
        else:
            todo.append(track)
    return todo, data_errors, done, total


def _journal_path() -> Path:
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    return JOURNAL_DIR / f"{date}-modal-asr-farm.jsonl"


def _append_journal(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# ----- remote GPU class ------------------------------------------------------


@app.cls(
    gpu=GPU_KIND,
    timeout=TASK_TIMEOUT_S,
    max_containers=DEFAULT_MAX_CONTAINERS,
    secrets=_r2_secrets(),
)
class AsrWorker:
    @modal.enter()
    def load(self) -> None:
        from faster_whisper import WhisperModel

        self.model = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")

    @modal.method()
    def transcribe_track(
        self, stable_id: str, vocals_key: str, vocals_sha256: str, bucket: str
    ) -> dict[str, Any]:
        """One track. Returns success fields or {stable_id, error}; never raises."""
        import tempfile

        t0 = time.perf_counter()
        try:
            client = _r2_client()
            resp = client.get_object(Bucket=bucket, Key=vocals_key)
            audio_bytes = resp["Body"].read()
            digest = hashlib.sha256(audio_bytes).hexdigest()
            if digest != vocals_sha256:
                raise ValueError(
                    f"index digest {vocals_sha256} != object digest {digest}"
                )

            suffix = Path(vocals_key).suffix or ".audio"
            with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
                tmp.write(audio_bytes)
                tmp.flush()
                segments, info = self.model.transcribe(
                    tmp.name,
                    language=None,
                    word_timestamps=True,
                    condition_on_previous_text=False,
                    vad_filter=False,
                    beam_size=5,
                )
                words: list[dict[str, Any]] = []
                text_parts: list[str] = []
                for seg in segments:
                    text_parts.append(seg.text)
                    for word in seg.words or []:
                        words.append({
                            "word": word.word.strip(),
                            "start_s": word.start,
                            "end_s": word.end,
                            "prob": word.probability,
                        })

            payload = {
                "schema_version": SCHEMA_VERSION,
                "stable_id": stable_id,
                "vocals_sha256": vocals_sha256,
                "model": MODEL_LABEL,
                "generated_at": datetime.now(UTC).isoformat(),
                "language": info.language,
                "language_probability": info.language_probability,
                "duration_s": info.duration,
                "text": "".join(text_parts).strip(),
                "words": words,
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            out_key = _output_key(stable_id)
            client.put_object(Bucket=bucket, Key=out_key, Body=body)
            remote = client.head_object(Bucket=bucket, Key=out_key)["ContentLength"]
            if remote != len(body):
                raise RuntimeError(
                    f"r2 short write: {bucket}/{out_key} is {remote} bytes, sent {len(body)}"
                )
        except Exception as exc:
            return {
                "stable_id": stable_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "stable_id": stable_id,
            "word_count": len(words),
            "gpu_s": time.perf_counter() - t0,
            "language": info.language,
            "language_probability": info.language_probability,
        }


# ----- subcommands -----------------------------------------------------------


def _print_data_errors(data_errors: list[str]) -> None:
    for stable_id in data_errors:
        print(
            f"[WARN] data error: {stable_id} "
            "(expected exactly one vocals.mp3 or vocals.flac)"
        )


def cmd_plan(args: argparse.Namespace) -> int:
    _require_r2_env()
    client = _r2_client()
    bucket = _r2_bucket()
    index = _load_index(client, bucket)
    todo, data_errors, done, total = _classify_index(client, bucket, index, args.force)
    _print_data_errors(data_errors)
    print(
        f"[PLAN] total={total} done={done} todo={len(todo)} "
        f"data_errors={len(data_errors)}"
    )
    return 0


def _todo_generator(todo: list[VocalsTrack], bucket: str) -> Iterator[tuple[str, str, str, str]]:
    for track in todo:
        yield (
            track.stable_id,
            _content_addressed_key(track.vocals_sha256),
            track.vocals_sha256,
            bucket,
        )


def cmd_run(args: argparse.Namespace) -> int:
    _require_r2_env()
    client = _r2_client()
    bucket = _r2_bucket()
    index = _load_index(client, bucket)
    todo, data_errors, done, total = _classify_index(client, bucket, index, args.force)
    _print_data_errors(data_errors)
    if args.limit is not None:
        todo = todo[: args.limit]
    print(
        f"[RUN] total={total} done={done} todo={len(todo)} "
        f"data_errors={len(data_errors)} bucket={bucket}"
    )
    if not todo:
        print("[DONE] transcribed=0 failed=0 wall=0s")
        return 0

    journal_path = _journal_path()
    transcribed = failed = 0
    t_run = time.monotonic()
    batch_size = len(todo)

    with app.run():
        worker = AsrWorker.with_options(max_containers=args.max_containers)()
        for result in worker.transcribe_track.starmap(
            _todo_generator(todo, bucket), order_outputs=False
        ):
            stable_id = result["stable_id"]
            if "error" in result:
                failed += 1
                print(f"[ERROR] {stable_id}: {result['error']}", file=sys.stderr)
                _append_journal(
                    journal_path,
                    {
                        "stable_id": stable_id,
                        "ok": False,
                        "error": result["error"],
                        "word_count": None,
                        "gpu_s": None,
                    },
                )
                continue
            transcribed += 1
            word_count = result["word_count"]
            gpu_s = result["gpu_s"]
            lang = result["language"]
            lang_p = result["language_probability"]
            print(
                f"[OK] {transcribed + failed}/{batch_size} {stable_id}: "
                f"{word_count} asr words ({lang} p={lang_p:.2f}) in {gpu_s:.1f}s gpu"
            )
            _append_journal(
                journal_path,
                {
                    "stable_id": stable_id,
                    "ok": True,
                    "error": None,
                    "word_count": word_count,
                    "gpu_s": gpu_s,
                },
            )

    wall_s = time.monotonic() - t_run
    print(f"[DONE] transcribed={transcribed} failed={failed} wall={wall_s:.0f}s")
    return 1 if failed else 0


def cmd_verify(args: argparse.Namespace) -> int:
    _require_r2_env()
    client = _r2_client()
    bucket = _r2_bucket()
    index = _load_index(client, bucket)

    valid_ids: list[str] = []
    data_errors: list[str] = []
    for stable_id, files in _iter_index_tracks(index):
        if _resolve_vocals(files) is None:
            data_errors.append(stable_id)
            continue
        valid_ids.append(stable_id)

    present = absent = 0
    sample_size = min(VERIFY_SAMPLE_SIZE, len(valid_ids))
    if sample_size == 0:
        print("[VERIFY] present=0 absent=0 (no valid stable_ids in index)")
    else:
        sampled = random.sample(valid_ids, sample_size)
        for stable_id in sampled:
            key = _output_key(stable_id)
            if _r2_head_exists(client, bucket, key):
                present += 1
                print(f"[PRESENT] {key}")
            else:
                absent += 1
                print(f"[ABSENT] {key}")
        print(f"[VERIFY] present={present} absent={absent}")

    control_present = _r2_head_exists(client, bucket, VERIFY_NEGATIVE_CONTROL)
    control_state = "PRESENT" if control_present else "ABSENT"
    print(f"[VERIFY] negative-control={control_state} ({VERIFY_NEGATIVE_CONTROL})")

    if sample_size > 0 and absent > 0:
        return 1
    if control_present:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="show how many tracks need transcription")
    plan.add_argument(
        "--force",
        action="store_true",
        help="treat existing lyrics-asr outputs as todo",
    )
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", help="transcribe todo tracks on Modal GPUs")
    run.add_argument("--limit", type=int, default=None, help="cap tracks processed")
    run.add_argument(
        "--max-containers",
        type=int,
        default=DEFAULT_MAX_CONTAINERS,
        help="Modal max_containers for the worker class",
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="re-transcribe even when the output key already exists",
    )
    run.set_defaults(func=cmd_run)

    verify = sub.add_parser(
        "verify", help="HEAD-sample outputs plus a negative control"
    )
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
