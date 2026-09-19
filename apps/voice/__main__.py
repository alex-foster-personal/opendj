"""CLI entrypoint for ``python -m apps.voice``.

Subcommands:
  run             -- start the mic -> wake -> stt -> dispatch -> tts daemon.
  probe           -- pipe transcripts through grammar + dispatch, no mic.
  bench           -- run the smoke harness and print percentile timings.
  list-devices    -- rich table of `sd.query_devices()`.
  say             -- subprocess `say` shortcut for smoke-testing TTS.

Plan 1 ships the skeleton; Plan 2 wires ``run`` to parse -> dispatch.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Sequence

from apps.voice import audio, tts


def _cmd_list_devices(_args: argparse.Namespace) -> int:
    """Print the list of audio devices via ``sd.query_devices()``."""
    try:
        sd = audio._sounddevice()  # lazy import so --help works without it
    except RuntimeError as exc:
        print(f"[voice] {exc}", file=sys.stderr)
        return 2
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(title="audio devices (sounddevice)")
        table.add_column("idx", justify="right")
        table.add_column("name")
        table.add_column("in", justify="right")
        table.add_column("out", justify="right")
        table.add_column("sr", justify="right")
        for idx, dev in enumerate(sd.query_devices()):
            table.add_row(
                str(idx),
                str(dev.get("name", "?")),
                str(dev.get("max_input_channels", 0)),
                str(dev.get("max_output_channels", 0)),
                f"{float(dev.get('default_samplerate', 0)):.0f}",
            )
        Console().print(table)
    except ImportError:
        # rich is a project dep (requirements.txt) but guard anyway.
        for idx, dev in enumerate(sd.query_devices()):
            print(f"{idx}\t{dev.get('name', '?')}")
    return 0


def _cmd_say(args: argparse.Namespace) -> int:
    engine = tts.make_tts()
    result = engine.speak(args.text)
    print(f"[voice] spoke via {result.backend}: {result.text!r}")
    return result.returncode


def _capture_transcripts(_args: argparse.Namespace):
    """Yield transcripts from the mic -> wake -> STT pipeline.

    Default implementation requires the audio stack (sounddevice + a wake
    backend + an STT client). Tests monkeypatch this generator with a
    finite iterable so `_cmd_run` exits after a deterministic number of
    iterations without touching real hardware.
    """
    from apps.voice import audio, stt, wake

    try:
        sd = audio._sounddevice()
    except RuntimeError:
        # VOICE-01 / Phase 5: tolerate hosts without PortAudio/sounddevice.
        # The daemon stays alive (in text-only mode) instead of bailing rc=2.
        print(
            "[voice] mic capture disabled: sounddevice not importable",
            file=sys.stderr,
            flush=True,
        )
        return
    try:
        backend = wake.make_backend()
    except (ImportError, RuntimeError) as exc:
        # VOICE-01, same degrade contract as the sounddevice branch above.
        # openWakeWord's default tflite runtime has no wheel on every host
        # (macOS arm64 among them), so a host can have a working mic and
        # still have no loadable wake model. Name the reason on stderr and
        # stay in text-only mode rather than crashing the daemon; flip
        # WAKE_BACKEND=stub or install the runtime to re-enable it.
        print(
            f"[voice] wake word disabled: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return
    client = stt.make_client()
    sample_rate = 16_000
    frame_ms = 30
    frame_len = int(sample_rate * frame_ms / 1000)
    ring = audio.RingBuffer(capacity_frames=int(2_000 / frame_ms))  # ~2s
    with sd.RawInputStream(
        samplerate=sample_rate, channels=1, dtype="int16", blocksize=frame_len
    ) as stream:
        while True:
            frame, _ = stream.read(frame_len)
            ring.push(bytes(frame))
            frames = ring.pop_all()
            if wake.triggered(backend, frames):
                pcm = b"".join(frames)
                t = client.transcribe(pcm, sample_rate_hz=sample_rate)
                if t and getattr(t, "text", "").strip():
                    yield t.text


def _cmd_run(args: argparse.Namespace) -> int:
    """Start the voice daemon: wake-word -> STT -> grammar -> dispatch."""
    from apps.voice import actions, bus, grammar
    from apps.voice import context as ctx_mod

    # P14-F03 (retained from master #102): honour --dry-bus and
    # --enable-destructive on the `run` subcommand. Previously both flags
    # were silently ignored, so an operator asking for the JSONL stub bus
    # or for destructive mode had no way to confirm their flag landed.
    event_bus = bus.make_bus(force_stub=bool(getattr(args, "dry_bus", False)))
    ctx = ctx_mod.VoiceContext.from_env(event_bus=event_bus)
    if getattr(args, "enable_destructive", False):
        ctx.destructive = True
    registry = actions.default_registry()

    print(
        f"[voice] daemon starting (mute_until={ctx.mute_until!r} "
        f"destructive={ctx.destructive} dry_bus={bool(getattr(args, 'dry_bus', False))})"
    )
    if args.echo:
        print("[voice] --echo mode requested; transcripts will be echoed.")

    max_iters = getattr(args, "max_iters", None)
    iters = 0

    # Install a SIGTERM handler that flips a flag checked by the capture loop
    # (and, more importantly, converts SIGTERM into a KeyboardInterrupt-like
    # clean exit instead of the default abrupt termination).
    _shutdown = {"requested": False}

    def _on_sigterm(signum, frame):  # noqa: ARG001
        _shutdown["requested"] = True
        print("[voice] SIGTERM received; shutting down", flush=True)

    prev_handler = None
    try:
        prev_handler = signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        # signal.signal() only works on the main thread; tests that invoke
        # _cmd_run from a worker thread should still succeed.
        prev_handler = None

    # Phase 5/VOICE-01: when sounddevice is unavailable, ``_capture_transcripts``
    # prints a single-line warning and returns immediately, so the daemon
    # degrades to text-only mode instead of bailing rc=2. CI / fresh installs
    # can therefore run ``voice run`` without PortAudio.
    try:
        for transcript in _capture_transcripts(args):
            iters += 1
            if args.echo:
                print(f"[voice] heard: {transcript!r}")
            intent = grammar.parse(transcript)
            if intent is None:
                print(f"[voice] grammar miss: {transcript!r}")
            else:
                response = registry.dispatch(intent, ctx)
                print(
                    f"[voice] {intent.kind} -> {response.reply!r} "
                    f"(published={response.published})"
                )
            if _shutdown["requested"]:
                break
            if max_iters is not None and iters >= max_iters:
                break
    except KeyboardInterrupt:
        print("[voice] interrupted; shutting down")
    except RuntimeError as exc:
        # Audio stack present-but-broken at runtime (e.g. whisper missing
        # mid-stream). Keep the legacy rc=2 contract for that case.
        print(f"[voice] daemon error: {exc}", file=sys.stderr)
        return 2
    finally:
        if prev_handler is not None:
            try:
                signal.signal(signal.SIGTERM, prev_handler)
            except (ValueError, OSError):
                pass
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    """Read one or more transcripts from --text / stdin; print events."""
    from apps.voice import actions, bus, grammar
    from apps.voice import context as ctx_mod

    transcripts: list[str] = []
    if args.text:
        transcripts.append(args.text)
    else:
        for line in sys.stdin:
            s = line.strip()
            if s:
                transcripts.append(s)

    event_bus = bus.make_bus(force_stub=args.dry_bus)
    rec_tts = tts.RecordingTts()
    ctx = ctx_mod.VoiceContext.from_env(event_bus=event_bus, tts_engine=rec_tts)
    registry = actions.default_registry()

    out: list[dict] = []
    for transcript in transcripts:
        intent = grammar.parse(transcript)
        if intent is None:
            print(
                json.dumps(
                    {"transcript": transcript, "intent": None, "response": "grammar_miss"},
                    sort_keys=True,
                )
            )
            continue
        response = registry.dispatch(intent, ctx)
        event_dict = {
            "transcript": transcript,
            "intent": intent.kind,
            "slots": intent.slots,
            "response": response.reply,
            "published": response.published,
            "dry_run": response.dry_run,
        }
        out.append(event_dict)
        print(json.dumps(event_dict, sort_keys=True))
    if not out and not transcripts:
        print("[voice] no transcripts supplied (use --text or pipe stdin)")
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    """Run the grammar + dispatch loop N times; print percentile timings."""
    import statistics

    from apps.voice import actions, bus, grammar
    from apps.voice import context as ctx_mod

    sample = args.text or "find daft punk"
    event_bus = bus.make_bus(force_stub=True)
    rec_tts = tts.RecordingTts()
    ctx = ctx_mod.VoiceContext.from_env(event_bus=event_bus, tts_engine=rec_tts)
    registry = actions.default_registry()
    durations: list[float] = []
    for _ in range(args.iterations):
        import time as _t
        t0 = _t.perf_counter()
        intent = grammar.parse(sample)
        if intent:
            registry.dispatch(intent, ctx)
        durations.append((_t.perf_counter() - t0) * 1000)
    durations.sort()
    p50 = durations[len(durations) // 2]
    p95 = durations[min(int(len(durations) * 0.95), len(durations) - 1)]
    print(
        json.dumps(
            {
                "iterations": args.iterations,
                "sample": sample,
                "p50_ms": round(p50, 3),
                "p95_ms": round(p95, 3),
                "max_ms": round(max(durations), 3),
                "mean_ms": round(statistics.fmean(durations), 3),
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apps.voice",
        description="Voice commands daemon + diagnostics (Phase 14).",
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    sp = sub.add_parser("run", help="start the voice daemon")
    sp.add_argument("--echo", action="store_true", help="echo-transcribe smoke mode")
    sp.add_argument(
        "--enable-destructive",
        action="store_true",
        help="allow SAVE_CUE / RATE_TRACK to publish non-dry-run events",
    )
    sp.add_argument("--dry-bus", action="store_true", help="force the JSONL stub bus")
    sp.add_argument(
        "--max-iters",
        type=int,
        default=None,
        help="exit after N dispatched transcripts (smoke-test / launcher hook)",
    )
    sp.add_argument(
        "--allow-low-memory",
        action="store_true",
        help="skip the 12 GB RAM pre-flight check",
    )
    sp.set_defaults(func=_cmd_run)

    sp = sub.add_parser("probe", help="parse a transcript through grammar + dispatch")
    sp.add_argument("--text", help="transcript to parse (else read stdin lines)")
    sp.add_argument("--dry-bus", action="store_true", help="force the JSONL stub bus")
    sp.set_defaults(func=_cmd_probe)

    sp = sub.add_parser("bench", help="run the grammar+dispatch loop N times")
    sp.add_argument("--iterations", type=int, default=10)
    sp.add_argument("--text", default=None)
    sp.set_defaults(func=_cmd_bench)

    sp = sub.add_parser("list-devices", help="list audio devices via sounddevice")
    sp.set_defaults(func=_cmd_list_devices)

    sp = sub.add_parser("say", help="speak a string via the `say` subprocess")
    sp.add_argument("text")
    sp.set_defaults(func=_cmd_say)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
