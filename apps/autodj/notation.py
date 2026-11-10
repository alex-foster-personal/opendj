"""Mix score: the human-readable text form of a TransitionPlan (AGENT-5858).

One move per line, in the plan's own units (bars relative to the out window)::

    transition bass_swap_blend
    out D1 trk-out from 113 for 16
    in  D2 trk-in  from 17 for 16
    tempo sync

    @0       D2 play
    @0       D2 eq_low kill
    @0..8    D2 fader -> full
    @8       D1 eq_low kill
    @8       D2 eq_low flat
    @8..16   mix -> full
    @12..16  D1 fader -> 0
    @16      D1 stop

The plan JSON stays canonical. :func:`parse_score` builds that JSON and hands
it to :func:`apps.autodj.plan.parse_plan`, so a score is refused for exactly
the reasons a JSON plan is. :func:`print_score` writes the one canonical text
for a plan, so ``parse_score(print_score(plan))`` returns the same plan
(compared by ``to_json`` plus the extension keys in ``extra``). Text
round-trips up to formatting: spacing, comments and the value words are
normalized.

Grammar, line by line (``#`` starts a comment outside a quoted string):

* ``transition <style>``
* ``out D<n> <track> from <bar> for <bars>`` and the same for ``in``; the ``in``
  line may end ``trim <dB|none> ["<reason>"]`` for the incoming gain trim
* ``tempo sync`` | ``tempo none`` (default sync), ``anchor <name>`` (default window)
* ``intent "<text>"`` and ``rationale "<text>"`` (JSON string literals)
* ``extra {"<key>": <json>}``: one extension key the plan JSON carries beyond
  the fields above, as a one-key JSON object filling the rest of the line (no
  comment after it)

Each header line may appear once; a repeated one is refused, never overwritten.
* a move: ``@<bar>`` or a range ``@<from>..<to>`` (also ``@<from>+<bars>``),
  then ``D1``..``D4`` or ``mix`` (the crossfader), then ``play`` | ``stop`` |
  ``master``, ``<control> <value>`` (set), or ``<control> -> <value>`` (ramp
  across the range; ``mix -> <value>`` and ``mix <value>`` for the crossfader).
  Values are numbers or ``kill`` (0), ``flat`` (0.5), ``full`` (1).

:func:`render_grid` draws the bar grid view (lanes per deck and control, one
column per beat). It is a view: it is never parsed back.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.autodj.plan import ACTIONS, CONTROLS, MIXER_CONTROLS, NOISE_FIELDS, PlanError, TransitionPlan, parse_plan

#: The value words a score may use in place of a number.
VALUE_WORDS: dict[str, float] = {"kill": 0.0, "flat": 0.5, "full": 1.0}
# A quoted string, a comment to the end of the line, an unterminated quote (so
# it is refused, not dropped), or a bare word that stops at a quote or a hash.
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|#.*|"\S*|[^\s"#]+')
_NUMBER = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_WHEN = re.compile(rf"^@({_NUMBER})(?:(\.\.|\+)({_NUMBER}))?$")
_DECK = re.compile(r"^D([1-4])$")
_TEMPO = {"sync": "sync_to_out", "none": "none"}
_BARE = re.compile(r'^[^\s"#\x00-\x1f\x7f]+$')


#: How many digits a window's bar may have. Far past any track yet exact past a
#: float's 2**53, and small enough that the whole-number conversion stays cheap.
MAX_WINDOW_DIGITS = 30
# A bar token longer than this is refused before Decimal parses it: leading or fractional zeros can make a short
# value cost a long parse, and the digit bound below cannot see them.
MAX_NUMBER_CHARS = 2 * MAX_WINDOW_DIGITS


class ScoreError(PlanError):
    """The text is not a mix score; the message names the line."""


def score_tokens(line: str) -> list[str]:
    """One score line's tokens, a ``#`` comment dropped (shared with the set score)."""
    tokens: list[str] = []
    for token in _TOKEN.findall(line):
        if token.startswith("#"):
            break
        tokens.append(token)
    return tokens


def score_text(token: str, where: str) -> str:
    """A bare or quoted word token as text; ``where`` names the line in the error."""
    if token.startswith('"'):
        try:
            value = json.loads(token)
        except ValueError as error:
            raise ScoreError(f"{where}: {token} is not a valid quoted string") from error
        if not isinstance(value, str):
            raise ScoreError(f"{where}: {token} is not a string")
        return value
    return token


def _number(token: str, where: str) -> float:
    if token in VALUE_WORDS:
        return VALUE_WORDS[token]
    if not re.fullmatch(_NUMBER, token):
        raise ScoreError(f"{where}: {token!r} is not a number or one of {', '.join(VALUE_WORDS)}")
    return float(token)


def _whole(token: str, where: str) -> int:
    # Bars only: the control words (full, off...) are values, not positions.
    if len(token) > MAX_NUMBER_CHARS:
        raise ScoreError(f"{where}: {token!r} is longer than {MAX_NUMBER_CHARS} characters")
    if not re.fullmatch(_NUMBER, token):
        raise ScoreError(f"{where}: {token!r} is not a whole number of bars")
    try:
        value = Decimal(token)  # exact: a float would round a large bar count
    except InvalidOperation as error:  # an exponent past Decimal's own limits
        raise ScoreError(f"{where}: {token!r} is not a whole number of bars") from error
    # Bounded before int(): 1e1000000 is a valid Decimal whose int() is a
    # million-digit number.
    if value and value.adjusted() >= MAX_WINDOW_DIGITS:
        raise ScoreError(f"{where}: {token!r} has more than {MAX_WINDOW_DIGITS} digits")
    if value != value.to_integral_value():
        raise ScoreError(f"{where}: {token!r} must be a whole number of bars")
    return int(value)


def _deck(token: str, where: str) -> int:
    match = _DECK.fullmatch(token)
    if match is None:
        raise ScoreError(f"{where}: expected D1..D4 or mix, got {token!r}")
    return int(match.group(1))


def _side(tokens: list[str], where: str) -> dict[str, Any]:
    trimmed = tokens[0] == "in" and len(tokens) in (9, 10) and tokens[7] == "trim"
    if not (len(tokens) == 7 or trimmed) or tokens[3] != "from" or tokens[5] != "for":
        raise ScoreError(f"{where}: expected '{tokens[0]} D<n> <track> from <bar> for <bars>'")
    side: dict[str, Any] = {
        "deck": _deck(tokens[1], where),
        "track": score_text(tokens[2], where),
        "window": {"start_bar": _whole(tokens[4], where), "bars": _whole(tokens[6], where)},
    }
    if trimmed:
        if tokens[8] != "none" and not re.fullmatch(_NUMBER, tokens[8]):
            raise ScoreError(f"{where}: trim takes a number of dB or none, got {tokens[8]!r}")
        side["trim_db"] = None if tokens[8] == "none" else float(tokens[8])
        side["trim_reason"] = score_text(tokens[9], where) if len(tokens) == 10 else None
    return side


def _when(token: str, where: str) -> tuple[float, float | None]:
    match = _WHEN.fullmatch(token)
    if match is None:
        raise ScoreError(f"{where}: a move starts with @<bar> or @<from>..<to>, got {token!r}")
    start = float(match.group(1))
    if match.group(2) is None:
        return start, None
    end_or_length = float(match.group(3))
    over = end_or_length - start if match.group(2) == ".." else end_or_length
    if over <= 0:
        raise ScoreError(f"{where}: a range must end after it starts")
    return start, over


def _control_and_args(mixer: bool, rest: list[str], where: str) -> tuple[str, list[str]]:
    if mixer:
        (control,) = MIXER_CONTROLS
        return control, rest
    if not rest or rest[0] not in CONTROLS or rest[0] in MIXER_CONTROLS:
        raise ScoreError(f"{where}: expected play, stop, master or a deck control, got {' '.join(rest)!r}")
    return rest[0], rest[1:]


def _change(control: str, args: list[str], over: float | None, where: str) -> dict[str, Any]:
    """The set or ramp half of an event, from what follows the control name."""
    if control == "fx_noise":
        if not args or args[0] not in NOISE_FIELDS:
            raise ScoreError(f"{where}: fx_noise needs a field, one of {', '.join(NOISE_FIELDS)}")
        change = _change(args[0], args[1:], over, where)
        kind = "ramp" if "ramp" in change else "set"
        change[kind] = {"fx_noise": change[kind]}
        return change
    if len(args) == 2 and args[0] == "->":
        if over is None:
            raise ScoreError(f"{where}: a ramp needs a range, like @0..8")
        return {"ramp": {control: _number(args[1], where)}, "over_bars": over}
    if len(args) == 1:
        if over is not None:
            raise ScoreError(f"{where}: use '-> <value>' to ramp across a range")
        return {"set": {control: _number(args[0], where)}}
    raise ScoreError(f"{where}: expected '<value>' or '-> <value>' after {control}")


def _move(tokens: list[str], where: str) -> dict[str, Any]:
    if len(tokens) < 2:
        raise ScoreError(f"{where}: a move needs a deck (D1..D4 or mix) and what to do")
    at_bar, over = _when(tokens[0], where)
    mixer = tokens[1] == "mix"
    deck = None if mixer else _deck(tokens[1], where)
    rest = tokens[2:]
    if not mixer and len(rest) == 1 and rest[0] in ACTIONS:
        if over is not None:
            raise ScoreError(f"{where}: {rest[0]} happens on one bar, not across a range")
        return {"at_bar": at_bar, "deck": deck, "do": rest[0]}
    control, args = _control_and_args(mixer, rest, where)
    return {"at_bar": at_bar, "deck": deck, **_change(control, args, over, where)}


_HEADER_FIELDS = {"out": "out", "in": "in", "transition": "style", "tempo": "tempo", "anchor": "anchor"}
_HEADER_FIELDS.update(intent="intent", rationale="rationale")
#: Plan keys a score writes as header or move lines, so never as ``extra``.
_PLAN_KEYS = frozenset(_HEADER_FIELDS.values()) | {"events"}


def _header(document: dict[str, Any], tokens: list[str], where: str) -> bool:
    """Apply one header line; False when the line is not a header this grammar knows."""
    head, pair = tokens[0], len(tokens) == 2
    if head in ("out", "in"):
        document[head] = _side(tokens, where)
    elif head == "transition" and pair:
        document["style"] = score_text(tokens[1], where)
    elif head == "tempo" and pair and tokens[1] in _TEMPO:
        document["tempo"] = {"mode": _TEMPO[tokens[1]]}
    elif head == "anchor" and pair:
        document["anchor"] = tokens[1]
    elif head in ("intent", "rationale") and pair:
        document[head] = score_text(tokens[1], where)
    else:
        return False
    return True


_REQUIRED_LINES = {
    "style": "transition <style>",
    "out": "out D<n> <track> from <bar> for <bars>",
    "in": "in D<n> <track> from <bar> for <bars>",
}


class _DuplicateKey(ValueError):
    """A JSON object that names one key twice, which the default decoder would collapse."""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen[key] = value
    return seen


def _extra(document: dict[str, Any], body: str, where: str) -> None:
    """One ``extra {"<key>": <json>}`` line into the document."""
    try:
        value, end = json.JSONDecoder(object_pairs_hook=_unique_pairs).raw_decode(body.strip())
    except _DuplicateKey as error:
        raise ScoreError(f"{where}: extra names {error.args[0]!r} twice; give each key once") from error
    except RecursionError as error:
        raise ScoreError(f"{where}: extra's JSON is nested too deeply") from error
    except ValueError as error:
        raise ScoreError(f"{where}: extra needs a one-key JSON object, like extra {{\"key\": 1}}") from error
    rest = body.strip()[end:].strip()
    if rest and not rest.startswith("#"):  # a trailing comment is fine, anything else is not
        raise ScoreError(f"{where}: extra needs a one-key JSON object, like extra {{\"key\": 1}}")
    if not isinstance(value, dict) or len(value) != 1:
        raise ScoreError(f"{where}: extra needs a one-key JSON object, like extra {{\"key\": 1}}")
    ((key, item),) = value.items()
    if key in _PLAN_KEYS:
        raise ScoreError(f"{where}: {key!r} has its own line; extra is for other keys")
    if key in document:
        raise ScoreError(f"{where}: a second extra line for {key!r}")
    document[key] = item


def score_document(text: str, *, first_line: int = 1) -> dict[str, Any]:
    """The plan JSON a score describes (not yet validated as a plan).

    ``first_line`` numbers the lines when the text is one block of a longer
    file (a set score), so an error still names the line the author sees.
    """
    return score_document_lines(text, first_line=first_line)[0]


def score_document_lines(text: str, *, first_line: int = 1) -> tuple[dict[str, Any], dict[str, int]]:
    """:func:`score_document` plus the line that wrote each field.

    The map's keys are :attr:`PlanError.path` values (``style``, ``out``,
    ``events[0]`` ...), so a plan error is placed by looking its path up,
    never by reading its message.
    """
    document: dict[str, Any] = {"events": []}
    lines: dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), start=first_line):
        where = f"line {number}"
        parts = raw.split(None, 1)
        if parts and parts[0] == "extra":
            _extra(document, parts[1] if len(parts) == 2 else "", where)
            continue
        tokens = score_tokens(raw)
        if not tokens:
            continue
        if tokens[0].startswith("@"):
            lines[f"events[{len(document['events'])}]"] = number
            document["events"].append(_move(tokens, where))
            continue
        if _HEADER_FIELDS.get(tokens[0]) in document:
            raise ScoreError(f"{where}: a second {tokens[0]!r} line; each header line may appear once")
        if not _header(document, tokens, where):
            raise ScoreError(f"{where}: cannot read {raw.strip()!r}")
        lines[_HEADER_FIELDS.get(tokens[0], tokens[0])] = number
    for required, line in _REQUIRED_LINES.items():
        if required not in document:
            raise ScoreError(f"missing the '{line}' line")
    return document, lines


def parse_score(text: str) -> TransitionPlan:
    """A :class:`TransitionPlan` from a mix score, validated like a JSON plan."""
    return parse_plan(score_document(text))


def looks_like_score(text: str) -> bool:
    """A JSON plan starts with ``{``; anything else is read as a score."""
    return not text.lstrip().startswith("{")


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _value_text(control: str, value: float) -> str:
    """EQ values print as words where one fits exactly; everything else as a number."""
    if control.startswith("eq_"):
        for word, exact in VALUE_WORDS.items():
            if value == exact:
                return word
    return _fmt(value)


def score_quoted(value: str) -> str:
    """``value`` bare when a score can read it back bare, else JSON-quoted."""
    # isprintable() also refuses Unicode format and separator characters, such as the bidi override U+202E.
    return value if _BARE.fullmatch(value) and value.isprintable() else json.dumps(value)


def _event_value(event: Any) -> float:
    """A set or ramp event's value; one with none is refused, never printed as 0."""
    if event.value is None:
        raise PlanError(f"a {event.kind} of {event.name} at bar {_fmt(event.at_bar)} has no value")
    return float(event.value)


def _when_text(at_bar: float, over: float) -> str:
    end = at_bar + over
    # The range form only when it reads back exactly; else the length form.
    if float(_fmt(end)) - float(_fmt(at_bar)) == over:
        return f"@{_fmt(at_bar)}..{_fmt(end)}"
    return f"@{_fmt(at_bar)}+{_fmt(over)}"


def _event_text(event: Any) -> str:
    when = _when_text(event.at_bar, event.over_bars) if event.kind == "ramp" else f"@{_fmt(event.at_bar)}"
    who = "mix" if event.deck is None else f"D{event.deck}"
    # At least one space after the time, however long it prints.
    if event.kind == "do":
        return f"{when:<8} {who} {event.name}"
    control = "" if event.name in MIXER_CONTROLS else f"{event.name} "
    if event.field is not None:
        control += f"{event.field} "
    arrow = "-> " if event.kind == "ramp" else ""
    return f"{when:<8} {who} {control}{arrow}{_value_text(event.name, _event_value(event))}"


def _extra_text(key: str, value: Any) -> str:
    """One ``extra`` line. :func:`parse_plan` bounds extension depth, so the encoder always has the stack."""
    return f"extra {json.dumps({key: value})}"


def _readable_window(head: str, *numbers: int) -> None:
    """Refuse a window the parser would not read back, so print and parse round-trip."""
    for number in numbers:
        if len(str(abs(number))) > MAX_WINDOW_DIGITS:
            raise ScoreError(f"{head} window {number} has more than {MAX_WINDOW_DIGITS} digits")


def print_score(plan: TransitionPlan) -> str:
    """The canonical mix score for ``plan``."""
    lines = [f"transition {score_quoted(plan.style)}"]
    for head, side in (("out", plan.out), ("in ", plan.in_)):
        _readable_window(head.strip(), side.start_bar, side.bars)
        line = f"{head} D{side.deck} {score_quoted(side.track)} from {side.start_bar} for {side.bars}"
        if side is plan.in_ and (side.trim_db is not None or side.trim_reason is not None):
            line += " trim " + ("none" if side.trim_db is None else _fmt(side.trim_db))
            line += "" if side.trim_reason is None else f" {json.dumps(side.trim_reason)}"
        lines.append(line)
    lines.append("tempo sync" if plan.tempo_mode == "sync_to_out" else "tempo none")
    if plan.anchor != "window":
        lines.append(f"anchor {plan.anchor}")
    lines.extend(f"{name} {json.dumps(getattr(plan, name))}" for name in ("intent", "rationale") if getattr(plan, name) is not None)
    if plan.bass_entry is not None:  # D11 fields main added; no header line of their own, so carried as extra
        lines.append(_extra_text("bass_entry", plan.bass_entry.to_json()))
    if plan.shape is not None:
        lines.append(_extra_text("shape", plan.shape))
    lines.extend(_extra_text(key, value) for key, value in plan.extra.items())
    lines.append("")
    lines.extend(_event_text(event) for event in plan.events)
    return "\n".join(lines) + "\n"


#: Lane-name column width in the grid view.
_LANE_WIDTH = 12
_LEGEND = "one column = one beat; [ -> v ===] ramps to v across the range, |v sets v"


def _put(row: list[str], at: int, text: str) -> None:
    for offset, char in enumerate(text):
        if at + offset < len(row):
            row[at + offset] = char


def _lane_name(event: Any) -> str:
    who = "mix" if event.deck is None else f"D{event.deck}"
    if event.kind == "do":
        return f"{who} deck"
    lane = "xfader" if event.name in MIXER_CONTROLS else event.name
    return f"{who} {lane}" if event.field is None else f"{who} {lane}.{event.field}"


def _lane_rank(name: str) -> tuple[str, int, str]:
    who, _, what = name.partition(" ")
    return ("~" if who == "mix" else who, 0 if what == "deck" else 1, what)


# The widest span the grid draws. One column per beat, so 512 bars is a
# 2049-column line per lane; a longer plan is refused before any lane exists.
MAX_GRID_BARS = 512


class _Grid:
    """Columns are beats from the earliest bar the plan touches (at most 0)."""

    def __init__(self, plan: TransitionPlan, beats_per_bar: int) -> None:
        self.bars = plan.bars
        self.per = beats_per_bar
        ends = [event.at_bar + event.over_bars for event in plan.events]
        if not all(math.isfinite(bar) for bar in (*ends, *(event.at_bar for event in plan.events))):
            raise ScoreError("an event starts or ends at an infinite bar: the grid cannot place it")
        self.first = min(0, *(int(event.at_bar // 1) for event in plan.events))
        # Events past the window widen the grid rather than being drawn at its edge.
        self.last = max(self.bars, *(math.ceil(end) for end in ends))
        if self.last - self.first > MAX_GRID_BARS:
            raise ScoreError(
                f"the grid spans bars {self.first}..{self.last}, more than {MAX_GRID_BARS} bars: "
                "show it as score or json instead"
            )
        self.columns = (self.last - self.first) * beats_per_bar + 1
        self.lanes: dict[str, list[str]] = {}

    def col(self, bar: float) -> int:
        return max(0, min(self.columns - 1, round((bar - self.first) * self.per)))

    def mark(self, event: Any) -> None:
        row = self.lanes.setdefault(_lane_name(event), [" "] * (self.columns + _LANE_WIDTH))
        start = self.col(event.at_bar)
        if event.kind == "do":
            _put(row, start, event.name)
            return
        value = _value_text(event.name, _event_value(event))
        if event.kind == "set":
            _put(row, start, f"|{value}")
            return
        end = self.col(event.at_bar + event.over_bars)
        row[start : end + 1] = ["="] * (end - start + 1)
        row[start], row[end] = "[", "]"
        label = f" -> {value} "
        if end - start > len(label) + 1:
            _put(row, start + 1, label)

    def lines(self) -> list[str]:
        ruler = [" "] * (self.columns + 4)
        ticks = [" "] * self.columns
        for bar in range(self.first, self.last + 1):
            if bar % 4 == 0:
                _put(ruler, self.col(bar), str(bar))
                ticks[self.col(bar)] = "|"
        out = [f"{'bar':<{_LANE_WIDTH}}{''.join(ruler).rstrip()}", f"{'':<{_LANE_WIDTH}}{''.join(ticks).rstrip()}"]
        out.extend(f"{name:<{_LANE_WIDTH}}{''.join(self.lanes[name]).rstrip()}" for name in sorted(self.lanes, key=_lane_rank))
        return out


def render_grid(plan: TransitionPlan, *, beats_per_bar: int = 4) -> str:
    """The bar grid view: one lane per deck and control, one column per beat."""
    grid = _Grid(plan, beats_per_bar)
    for event in plan.events:
        grid.mark(event)
    return "\n".join([*grid.lines(), "", _LEGEND]) + "\n"
