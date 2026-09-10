"""The one merged `python -m apps.lyrics` parser: shipping + eval subcommands.

Main carried fetch/index; the karaoke branch carried stats/score/crosscheck/
witness-eval behind a parser-wide --dataset-dir. Landing merged them into one
parser, so these pin the shape that merge chose.

- if a shipping subcommand (fetch, index) disappears then broken
- if an eval subcommand (stats, score, crosscheck, witness-eval) disappears then broken
- if --dataset-dir leaks onto fetch/index, where it means nothing, then broken
- if the score join stops normalizing song names then accented songs drop
  silently out of the denominator -- broken
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from apps.lyrics.__main__ import _song_key, build_parser

SHIPPING = ("fetch", "index")
EVAL = ("stats", "score", "crosscheck", "witness-eval")

EVAL_ARGV = {
    "stats": ["stats"],
    "score": ["score", "--pred", "preds"],
    "crosscheck": ["crosscheck", "--pred", "preds", "--asr", "asr"],
    "witness-eval": ["witness-eval", "--pred", "preds", "--asr", "asr"],
}


@pytest.mark.parametrize("argv", [["fetch", "abc123"], ["index", "--once"]])
def test_shipping_subcommands_survive_the_merge(argv: list[str]) -> None:
    assert build_parser().parse_args(argv).command == argv[0]


@pytest.mark.parametrize("command", EVAL)
def test_eval_subcommands_landed(command: str) -> None:
    args = build_parser().parse_args(EVAL_ARGV[command])
    assert args.command == command
    assert args.dataset_dir.name == "jamendolyrics", "the ground-truth default must survive"


def test_dataset_dir_is_per_eval_subcommand_not_parser_wide() -> None:
    """If it leaked onto fetch/index they would advertise a flag they ignore."""
    assert build_parser().parse_args(["stats", "--dataset-dir", "/tmp/gt"]).dataset_dir == Path(
        "/tmp/gt"
    )
    for command in SHIPPING:
        with pytest.raises(SystemExit):
            build_parser().parse_args([command, "--dataset-dir", "/tmp/gt"])


def test_song_key_joins_across_unicode_forms() -> None:
    """APFS hands back NFD filenames; the dataset CSV holds NFC."""
    name = "Capotes_à_un_Franc_-_elmanu"
    assert unicodedata.normalize("NFD", name) != name, "fixture must actually differ by form"
    assert _song_key(unicodedata.normalize("NFD", name)) == _song_key(name)
