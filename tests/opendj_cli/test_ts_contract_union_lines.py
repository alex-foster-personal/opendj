"""The TypeScript union reader keeps working when the formatter wraps a member.

Prettier wraps a union member onto several lines once it passes the print
width (PR #2746 did this to ``play``), and a one-line reader then fails to
collect every test that imports the contract.
"""

from pathlib import Path

from tests.opendj_cli import ts_contract

_WRAPPED_UNION = """\
export type PerformanceCommand =
\t| { type: 'unload'; deck: DeckId; refuseIfMaster?: boolean }
\t| {
\t\t\ttype: 'play';
\t\t\tdeck: DeckId;
\t\t\tplaying: boolean;
\t\t\tstart_at_context_sec?: number;
\t  }
\t// a comment between members
\t| { type: 'loop'; deck: DeckId; loop: { in_ms: number; out_ms: number } | null };

export interface Next {}
"""


def test_wrapped_member_joins_into_one_line(tmp_path: Path) -> None:
    source = tmp_path / "ipc.ts"
    source.write_text(_WRAPPED_UNION, encoding="utf-8")
    members = ts_contract._union_lines(source, ts_contract._COMMAND_UNION)
    assert members == [
        "| { type: 'unload'; deck: DeckId; refuseIfMaster?: boolean }",
        "| { type: 'play'; deck: DeckId; playing: boolean; start_at_context_sec?: number }",
        "| { type: 'loop'; deck: DeckId; loop: { in_ms: number; out_ms: number } | null };",
    ]


def test_wrapped_member_fields_parse(tmp_path: Path) -> None:
    source = tmp_path / "ipc.ts"
    source.write_text(_WRAPPED_UNION, encoding="utf-8")
    play = ts_contract._union_lines(source, ts_contract._COMMAND_UNION)[1]
    assert ts_contract._command_member(play) == "play"


def test_unclosed_member_fails_loud(tmp_path: Path) -> None:
    source = tmp_path / "ipc.ts"
    source.write_text("export type PerformanceCommand =\n\t| {\n\t\ttype: 'play';\n", encoding="utf-8")
    try:
        ts_contract._union_lines(source, ts_contract._COMMAND_UNION)
    except AssertionError as error:
        assert "never closes" in str(error)
    else:
        raise AssertionError("an unclosed union member must fail, not parse")


def test_live_command_union_parses() -> None:
    assert "play" in ts_contract.command_fields()
    assert ts_contract.command_fields()["play"]["start_at_context_sec"] is True


def test_live_load_union_ignores_inline_comments_between_fields() -> None:
    load = ts_contract.command_fields()["load"]
    assert load["stable_id"] is False
    assert load["refuseIfMaster"] is True
    assert load["stems"] is True
    assert load["suppressCommandErrorToast"] is True


def test_a_comment_with_a_semicolon_is_dropped_but_a_url_literal_survives(tmp_path: Path) -> None:
    """Both directions of comment stripping in a wrapped member.

    A `;` inside a line comment must not split off a fragment that swallows the next
    field, and the stripper must not overshoot: `//` inside a string literal type is
    part of the field, not a comment."""
    source = tmp_path / "ipc.ts"
    source.write_text(
        "export type PerformanceCommand =\n"
        "\t| {\n"
        "\t\t\ttype: 'open';\n"
        "\t\t\thref: 'https://example.com/a';\n"
        "\t\t\t// shown by the caller (skip); errors still update.\n"
        "\t\t\tsuppressToast?: boolean;\n"
        "\t  };\n",
        encoding="utf-8",
    )
    [member] = ts_contract._union_lines(source, ts_contract._COMMAND_UNION)
    assert member == "| { type: 'open'; href: 'https://example.com/a'; suppressToast?: boolean };"
