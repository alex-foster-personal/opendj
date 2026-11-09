"""``opendj practice <device> [--target <control id>]``: open practice mode (ADR-0137).

``opendj practice <device> --compare`` opens the compare route at
``<ui_url>/practice/<device>/compare``; optional ``--ref``, ``--seed``, ``--scale`` set query
params. ``--trials`` and ``--answer`` print JSON from the same blind-trial state as the page
(issue #9406), using ``GET /api/v1/practice/devices/<device>`` for the panel.

Same route a person reaches by URL: ``<ui_url>/practice/<device>``. The device must ship a
panel layout, checked against ``GET /api/v1/practice/devices`` before the browser opens, so
an unknown device fails here with the list of known ones instead of opening an error page.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.client import EngineClient
from apps.opendj_cli.origin import EngineIdentityMismatch, EngineNotRunning, resolve_verified_origin
from apps.opendj_cli.practice_compare import (
    blind_trials,
    build_compare_report,
    compare_scale_px_per_mm,
    record_answer,
)


class PracticeUsage(ValueError):
    pass


@dataclass(frozen=True)
class CompareArgs:
    compare: bool
    ref: str | None
    seed: int | None
    scale: float | None
    trials: bool
    answers: tuple[tuple[str, str, int], ...]


_ANSWER_TOKEN_RE = re.compile(r"^t\d+=([AB]):(\d+)$")


def practice_url(
    ui_url: str,
    device: str,
    target: str | None,
    *,
    compare: bool = False,
    ref: str | None = None,
    seed: int | None = None,
    scale: float | None = None,
) -> str:
    base = f"{ui_url.rstrip('/')}/practice/{urllib.parse.quote(device)}"
    path = f"{base}/compare" if compare else base
    params: list[tuple[str, str]] = []
    if not compare and target:
        params.append(("target", target))
    if compare:
        if ref:
            params.append(("ref", ref))
        if seed is not None:
            params.append(("seed", str(seed)))
        if scale is not None:
            params.append(("scale", str(scale)))
    if params:
        return f"{path}?{urllib.parse.urlencode(params)}"
    return path


def _parse_seed_u32(raw: str) -> int:
    try:
        n = int(raw, 10)
    except ValueError as exc:
        raise PracticeUsage("--seed needs a non-negative integer below 2**32") from exc
    if n < 0 or n >= 2**32:
        raise PracticeUsage("--seed needs a non-negative integer below 2**32")
    return n & 0xFFFFFFFF


def _parse_scale(raw: str) -> float:
    try:
        s = float(raw)
    except ValueError as exc:
        raise PracticeUsage("--scale needs a number greater than 0") from exc
    if not math.isfinite(s) or s <= 0:
        raise PracticeUsage("--scale needs a number greater than 0")
    return s


def _parse_answer_arg(raw: str) -> tuple[str, str, int]:
    m = _ANSWER_TOKEN_RE.match(raw)
    if not m:
        raise PracticeUsage("each --answer must look like t1=A:4 (trial id, A or B, rating 1 to 5)")
    letter = m.group(1)
    rating = int(m.group(2))
    if rating < 1 or rating > 5:
        raise PracticeUsage("rating in --answer must be from 1 to 5")
    tid = raw.split("=", 1)[0]
    return tid, letter, rating


def _require_compare(compare: bool, flag: str) -> None:
    if not compare:
        raise PracticeUsage(f"{flag} requires --compare")


@dataclass
class _CompareParseScratch:
    compare: bool = False
    ref: str | None = None
    seed: int | None = None
    scale: float | None = None
    trials: bool = False
    answers: list[tuple[str, str, int]] = field(default_factory=list)
    out: list[str] = field(default_factory=list)


def _take_value(tokens: list[str], i: int, err: str) -> tuple[str, int]:
    if i + 1 >= len(tokens):
        raise PracticeUsage(err)
    return tokens[i + 1], i + 2


def _apply_compare_flag(st: _CompareParseScratch, tokens: list[str], i: int) -> int:
    t = tokens[i]
    if t == "--compare":
        st.compare = True
        return i + 1
    if t == "--ref":
        _require_compare(st.compare, "--ref")
        v, j = _take_value(tokens, i, "--ref needs a path or URL")
        st.ref = v
        return j
    if t == "--seed":
        _require_compare(st.compare, "--seed")
        raw, j = _take_value(tokens, i, "--seed needs a non-negative integer below 2**32")
        st.seed = _parse_seed_u32(raw)
        return j
    if t == "--scale":
        _require_compare(st.compare, "--scale")
        raw, j = _take_value(tokens, i, "--scale needs a number greater than 0")
        st.scale = _parse_scale(raw)
        return j
    if t == "--trials":
        _require_compare(st.compare, "--trials")
        st.trials = True
        return i + 1
    if t == "--answer":
        _require_compare(st.compare, "--answer")
        raw, j = _take_value(tokens, i, "each --answer must look like t1=A:4 (trial id, A or B, rating 1 to 5)")
        st.answers.append(_parse_answer_arg(raw))
        return j
    st.out.append(t)
    return i + 1


def parse_compare_args(tokens: list[str]) -> tuple[list[str], CompareArgs]:
    """Strip compare-mode flags; return remaining tokens and a frozen ``CompareArgs``."""
    st = _CompareParseScratch()
    i = 0
    while i < len(tokens):
        i = _apply_compare_flag(st, tokens, i)

    if st.trials and st.answers:
        raise PracticeUsage("--trials cannot be combined with --answer")
    if st.trials and st.seed is None:
        raise PracticeUsage("--trials/--answer need --seed (read it from the page's query().seed)")
    if st.answers and st.seed is None:
        raise PracticeUsage("--trials/--answer need --seed (read it from the page's query().seed)")
    if st.compare and "--target" in st.out:
        raise PracticeUsage("usage: --compare and --target cannot be combined")

    return st.out, CompareArgs(
        compare=st.compare,
        ref=st.ref,
        seed=st.seed,
        scale=st.scale,
        trials=st.trials,
        answers=tuple(st.answers),
    )


def parse_practice_args(rest: Sequence[str]) -> tuple[str, str | None]:
    tokens = list(rest)
    target: str | None = None
    if "--target" in tokens:
        i = tokens.index("--target")
        if i + 1 >= len(tokens):
            raise PracticeUsage("--target needs a control id")
        target = tokens[i + 1]
        del tokens[i : i + 2]
    if len(tokens) != 1:
        raise PracticeUsage(
            "usage: opendj practice <device> [--target <control id>] | "
            "<device> --compare [--ref <path or url>] [--seed N] [--scale S] [--trials | --answer tN=L:R ...]"
        )
    return tokens[0], target


def resolve_ref(value: str) -> str:
    if value.startswith(("http://", "https://", "data:")):
        return value
    p = Path(value).expanduser()
    if not p.is_file():
        raise PracticeUsage(f"--ref {value!r} is not a file or an http(s) URL")
    return p.resolve().as_uri()


def _emit_usage(as_json: bool, msg: str) -> int:
    print(json.dumps({"error": "usage", "message": msg}) if as_json else f"[ERROR] {msg}")
    return EXIT_FAILED


def _run_compare_trials_json(client: EngineClient, device: str, seed_u32: int) -> None:
    layout = client.practice_layout(device)
    print(json.dumps({"device": device, "seed": seed_u32, "trials": blind_trials(layout["panel"], seed_u32)}))


def _run_compare_report_json(
    client: EngineClient,
    device: str,
    seed_u32: int,
    cmp_args: CompareArgs,
    ref_for_report: str,
    *,
    as_json: bool,
) -> int:
    layout = client.practice_layout(device)
    trials_state = blind_trials(layout["panel"], seed_u32)
    px = compare_scale_px_per_mm(cmp_args.scale)
    recorded = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    try:
        for tid, letter, rating in cmp_args.answers:
            trials_state = record_answer(trials_state, tid, letter, rating)
    except ValueError as error:
        return _emit_usage(as_json, str(error))
    report = build_compare_report(
        device=device,
        ref=ref_for_report,
        seed=seed_u32,
        px_per_mm=px,
        trials=trials_state,
        recorded_at=recorded,
    )
    print(json.dumps(report))
    return EXIT_CONFIRMED


def _dispatch_practice(
    client: EngineClient,
    device: str,
    target: str | None,
    cmp_args: CompareArgs,
    ref_resolved: str | None,
    *,
    as_json: bool,
) -> int:
    known = client.practice_devices()
    if device not in known:
        message = f"no practice layout for {device!r}; known: {', '.join(known)}"
        print(json.dumps({"error": "unknown_device", "message": message}) if as_json else f"[ERROR] {message}")
        return EXIT_FAILED

    if cmp_args.compare and cmp_args.trials:
        assert cmp_args.seed is not None
        _run_compare_trials_json(client, device, cmp_args.seed)
        return EXIT_CONFIRMED

    if cmp_args.compare and cmp_args.answers:
        assert cmp_args.seed is not None
        ref_for_report = ref_resolved if ref_resolved is not None else (cmp_args.ref or "")
        return _run_compare_report_json(client, device, cmp_args.seed, cmp_args, ref_for_report, as_json=as_json)

    url = practice_url(
        str(client.ui_address()["ui_url"]),
        device,
        target,
        compare=cmp_args.compare,
        ref=ref_resolved,
        seed=cmp_args.seed,
        scale=cmp_args.scale,
    )
    if webbrowser.open(url):
        print(json.dumps({"opened": True, "url": url}) if as_json else f"opened {url}")
        return EXIT_CONFIRMED
    print(
        json.dumps({"error": "browser_unavailable", "url": url}) if as_json else f"[ERROR] no browser could open {url}"
    )
    return EXIT_FAILED


def run(rest: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    as_json = as_json or "--json" in rest
    rest_list = [t for t in rest if t != "--json"]
    try:
        rest_list, cmp_args = parse_compare_args(rest_list)
        device, target = parse_practice_args(rest_list)
    except PracticeUsage as error:
        return _emit_usage(as_json, str(error))
    if cmp_args.compare and target is not None:
        return _emit_usage(as_json, "usage: --compare and --target cannot be combined")
    ref_resolved: str | None = None
    if cmp_args.compare and cmp_args.ref is not None:
        try:
            ref_resolved = resolve_ref(cmp_args.ref)
        except PracticeUsage as error:
            return _emit_usage(as_json, str(error))
    try:
        client = EngineClient(origin=resolve_verified_origin(lock))
    except (EngineNotRunning, EngineIdentityMismatch) as error:
        print(json.dumps({"error": "engine_not_running", "message": str(error)}) if as_json else f"[ERROR] {error}")
        return EXIT_NO_ENGINE
    return _dispatch_practice(client, device, target, cmp_args, ref_resolved, as_json=as_json)
