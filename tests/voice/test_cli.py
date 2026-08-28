"""Unit tests for apps.voice.__main__ CLI (VOICE-01)."""
from __future__ import annotations

import io
import json

import pytest

from apps.voice import __main__ as cli

pytestmark = pytest.mark.requirement("VOICE-01")


class TestBuildParser:
    def test_subcommands_registered(self):
        p = cli.build_parser()
        args = p.parse_args(["probe", "--text", "find X"])
        assert args.func == cli._cmd_probe
        assert args.text == "find X"

    def test_help_no_subcommand_returns_zero(self, capsys):
        rc = cli.main([])
        captured = capsys.readouterr()
        assert rc == 0
        assert "probe" in captured.out
        assert "run" in captured.out


class TestProbe:
    def test_probe_with_text_emits_event_json(self, capsys, monkeypatch, tmp_path):
        # Force JSONL stub bus to avoid touching the repo data/ dir.
        monkeypatch.setenv("VOICE_INPUT_DEVICE", "0")  # harmless
        rc = cli.main(["probe", "--text", "find daft punk", "--dry-bus"])
        captured = capsys.readouterr()
        assert rc == 0
        lines = [l for l in captured.out.splitlines() if l.strip().startswith("{")]
        assert lines, f"expected JSON lines, got: {captured.out!r}"
        payload = json.loads(lines[0])
        assert payload["intent"] == "SEARCH"
        assert payload["published"] is True
        assert payload["slots"]["query"] == "daft punk"

    def test_probe_grammar_miss(self, capsys, monkeypatch):
        rc = cli.main(["probe", "--text", "gibberish", "--dry-bus"])
        captured = capsys.readouterr()
        assert rc == 0
        lines = [l for l in captured.out.splitlines() if l.strip().startswith("{")]
        payload = json.loads(lines[0])
        assert payload["intent"] is None
        assert payload["response"] == "grammar_miss"

    def test_probe_reads_stdin(self, capsys, monkeypatch):
        fake_stdin = io.StringIO("mute voice\nunmute voice\n")
        monkeypatch.setattr("sys.stdin", fake_stdin)
        rc = cli.main(["probe", "--dry-bus"])
        out = capsys.readouterr().out
        assert rc == 0
        assert '"intent": "MUTE_VOICE"' in out
        assert '"intent": "UNMUTE_VOICE"' in out


class TestBench:
    def test_bench_prints_percentile_json(self, capsys):
        rc = cli.main(["bench", "--iterations", "5", "--text", "find x"])
        out = capsys.readouterr().out
        assert rc == 0
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["iterations"] == 5
        assert "p50_ms" in payload
        assert "p95_ms" in payload


class TestSay:
    def test_say_uses_make_tts(self, monkeypatch, capsys):
        """Route `say` through the recording backend so we don't spawn anything."""
        monkeypatch.setenv("TTS_BACKEND", "recording")
        rc = cli.main(["say", "hello booth"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "spoke via recording" in out
        assert "hello booth" in out


class TestRun:
    def test_run_starts_daemon(self, capsys, monkeypatch):
        """VOICE-01 / P14-F02: ``python -m apps.voice run`` must actually
        drive the wake -> grammar -> dispatch loop, not just print a banner.

        We monkeypatch ``_capture_transcripts`` with a finite generator so
        the daemon exits after one iteration without touching the mic.
        """
        seen: list[str] = []

        def fake_capture(args):
            yield "find daft punk"

        monkeypatch.setattr(cli, "_capture_transcripts", fake_capture)
        # Stub out dispatch so we don't depend on real action handlers.
        from apps.voice import actions as actions_mod

        class _RecResp:
            reply = "ok"
            published = False
            meta: dict = {}
            dry_run = False

        def _fake_dispatch(self, intent, ctx):
            seen.append(intent.kind)
            return _RecResp()

        monkeypatch.setattr(actions_mod.Registry, "dispatch", _fake_dispatch)

        rc = cli.main(["run", "--dry-bus", "--max-iters", "1"])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "daemon starting" in out
        assert seen == ["SEARCH"], seen

    def test_run_ok_without_sounddevice(self, capsys, monkeypatch):
        """VOICE-01: on a host without PortAudio/sounddevice, ``voice run``
        must degrade to text-only mode (single warning line, rc=0) instead
        of hard-failing rc=2. This recovers CI + fresh-install ergonomics.
        """
        from apps.voice import audio

        def _boom():
            raise RuntimeError(
                "sounddevice not importable; install PortAudio + the "
                "`sounddevice` wheel or re-run without mic capture"
            )

        monkeypatch.setattr(audio, "_sounddevice", _boom)

        rc = cli.main(["run", "--dry-bus", "--max-iters", "1"])
        captured = capsys.readouterr()
        assert rc == 0, (captured.out, captured.err)
        assert "daemon starting" in captured.out
        assert "mic capture disabled: sounddevice not importable" in captured.err
