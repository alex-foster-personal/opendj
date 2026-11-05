/**
 * ANIM-01..03: the dial animation expression, typed into a control's
 * right-click "Animate..." box and sent by agents as `animate_control`.
 *
 * It is NOT a new language. It is the tail of the CLI's existing ramp, the
 * AGENT-03 `ramp` order with an AGENT-04 Duration (agent-duration.ts):
 *
 *     opendj eq 1 low 0 --over 8bars --anchor next_downbeat
 *                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  <- the expression
 *
 *   expr     := target ( "--over" duration | "--anchor" anchor | "--clock" clock )*   (--over required, each once)
 *   target   := a number in 0..1 (bus control units; 0.5 is flat for EQ, TRIM and FILTER)
 *   duration := N unit, unit = beat(s) | bar(s) | phrase(s) | ms; N > 0, whole unless ms
 *   anchor   := next_beat | next_downbeat | next_phrase
 *   clock    := master | 1..4
 *
 * The Python twin is apps/opendj_cli/animate.py. Both are held to the same
 * cases and the same messages by tests/opendj_cli/fixtures/animate_expr_vectors.json.
 *
 * Requirements (mini-PRD):
 *   ✔︎ A valid expression yields the ramp `to` and the AGENT-04 Duration.
 *     [if] `0 --over 8bars --anchor next_downbeat` does not give to 0, 8 bars, next_downbeat [then] ⛔️
 *   ✔︎ An invalid one yields ONE line naming the failing token.
 *     [if] `@8 0` fails without naming `@8` [then] ⛔️
 *     [if] `0 --over 8.5bars` is accepted [then] ⛔️
 *
 * Pure: no Svelte, no DOM, no engine import.
 */
import type { Duration, DurationAnchor, DurationUnit } from './agent-duration';

export type AnimateControl =
	| 'eq_low'
	| 'eq_mid'
	| 'eq_high'
	| 'trim'
	| 'filter'
	| 'fader'
	| 'crossfader'
	| 'stem_vocal'
	| 'stem_instrumental'
	| 'stem_drums'
	| 'stem_bass';

type DeckId = 1 | 2 | 3 | 4;

interface ControlSpec {
	label: string;
	/** True for every control but the crossfader, which belongs to no deck. */
	perDeck: boolean;
	/** The example the hint line shows for this control. */
	example: string;
	command: (deck: DeckId, value: number) => Record<string, unknown>;
}

const eq = (band: 'low' | 'mid' | 'high', label: string): ControlSpec => ({
	label,
	perDeck: true,
	example: '0 --over 8bars --anchor next_downbeat',
	command: (deck, value) => ({ type: 'eq', deck, band, value })
});

const mixer = (type: 'trim' | 'filter' | 'fader', label: string, example: string): ControlSpec => ({
	label,
	perDeck: true,
	example,
	command: (deck, value) => ({ type, deck, value })
});

const stem = (part: 'vocal' | 'instrumental' | 'drums' | 'bass', label: string): ControlSpec => ({
	label,
	perDeck: true,
	example: '0 --over 4bars --anchor next_downbeat',
	command: (deck, value) => ({ type: 'stem_gain', deck, stem: part, value })
});

export const ANIMATE_CONTROLS: Readonly<Record<AnimateControl, ControlSpec>> = {
	eq_low: eq('low', 'EQ LOW'),
	eq_mid: eq('mid', 'EQ MID'),
	eq_high: eq('high', 'EQ HI'),
	trim: mixer('trim', 'TRIM', '0.5 --over 2bars'),
	filter: mixer('filter', 'FILTER', '0.2 --over 4bars --anchor next_downbeat'),
	fader: mixer('fader', 'FADER', '0 --over 16beats'),
	crossfader: {
		label: 'CROSSFADER',
		perDeck: false,
		example: '1 --over 8bars --anchor next_downbeat',
		command: (_deck, value) => ({ type: 'crossfader', value })
	},
	stem_vocal: stem('vocal', 'VOCAL'),
	stem_instrumental: stem('instrumental', 'INST'),
	stem_drums: stem('drums', 'DRUMS'),
	stem_bass: stem('bass', 'BASS')
};

export const ANIMATE_CONTROL_IDS = Object.keys(ANIMATE_CONTROLS) as AnimateControl[];

export type AnimateParse =
	| { ok: true; to: number; over: Duration }
	| { ok: false; token: string | null; message: string };

export interface AnimateRampPayload {
	command: Record<string, unknown>;
	to: number;
	over: Duration;
}

// ----- grammar -----

const TARGET_RE = /^(?:\d+(?:\.\d*)?|\.\d+)$/;
const DURATION_RE = /^(\d+(?:\.\d*)?|\.\d+)(ms|beats?|bars?|phrases?)$/;
const UNITS: Readonly<Record<string, DurationUnit>> = {
	ms: 'ms',
	beat: 'beats',
	beats: 'beats',
	bar: 'bars',
	bars: 'bars',
	phrase: 'phrases',
	phrases: 'phrases'
};
const ANCHORS: readonly DurationAnchor[] = ['next_beat', 'next_downbeat', 'next_phrase'];
const OPTIONS = ['--over', '--anchor', '--clock'] as const;
type Option = (typeof OPTIONS)[number];

const DURATION_HINT = '--over needs a duration like 4beats, 2bars, 1phrase or 500ms';

function _fail(token: string | null, message: string): AnimateParse {
	return { ok: false, token, message };
}

function _duration(raw: string): Pick<Duration, 'unit' | 'n'> | string {
	const match = DURATION_RE.exec(raw.toLowerCase());
	if (match === null) return DURATION_HINT;
	const n = Number(match[1]);
	const unit = UNITS[match[2]];
	if (!(n > 0)) return 'a duration must be greater than zero';
	if (unit !== 'ms' && !Number.isInteger(n)) return `${unit} must be a whole number`;
	return { unit, n };
}

/** Parse one expression. Never throws: the result says what failed and where. */
export function parseAnimateExpr(text: string): AnimateParse {
	const tokens = text.trim().split(/\s+/).filter((token) => token !== '');
	if (tokens.length === 0) return _fail(null, 'empty: type a target then --over, e.g. 0 --over 8bars');
	const [target, ...rest] = tokens;
	if (!TARGET_RE.test(target)) return _fail(target, 'expected a target value 0..1 first, e.g. 0 --over 8bars');
	const to = Number(target);
	if (to > 1) return _fail(target, 'target must be within 0..1 (0.5 is flat for EQ, TRIM and FILTER)');
	const seen = new Map<Option, string>();
	for (let index = 0; index < rest.length; index += 2) {
		const option = rest[index];
		if (!(OPTIONS as readonly string[]).includes(option)) {
			return _fail(option, 'unknown option; expected --over, --anchor or --clock');
		}
		if (seen.has(option as Option)) return _fail(option, 'given twice');
		const value = rest[index + 1];
		if (value === undefined) return _fail(option, 'missing its value');
		seen.set(option as Option, value);
	}
	const overRaw = seen.get('--over');
	if (overRaw === undefined) return _fail(null, 'missing --over <duration>, e.g. 0 --over 8bars');
	const duration = _duration(overRaw);
	if (typeof duration === 'string') return _fail(overRaw, duration);
	const over: Duration = { ...duration };
	const anchor = seen.get('--anchor');
	if (anchor !== undefined) {
		if (!(ANCHORS as readonly string[]).includes(anchor)) {
			return _fail(anchor, '--anchor must be next_beat, next_downbeat or next_phrase');
		}
		over.anchor = anchor as DurationAnchor;
	}
	const clock = seen.get('--clock');
	if (clock !== undefined) {
		if (clock === 'master') over.clock = 'master';
		else if (/^[1-4]$/.test(clock)) over.clock = Number(clock);
		else return _fail(clock, '--clock must be master or 1..4');
	}
	return { ok: true, to, over };
}

/** The ramp executor's refusal codes (agent-duration.ts, agent-orders.ts) in the box's words. */
const START_REFUSALS: Readonly<Record<string, string>> = {
	no_master: 'no_master: no deck is master - play a deck, or name one with --clock 1..4',
	clock_not_playing: 'clock_not_playing: the clock deck is stopped - play it, or use an ms duration',
	no_grid: 'no_grid: the clock deck has no beatgrid that far ahead - use fewer beats or bars, or ms',
	no_phrase: 'no_phrase: the clock deck has no analysed phrase that far ahead - use bars or ms'
};

/** A refusal at start, explained; any other message passes through unchanged. */
export function animateStartRefusal(message: string): string {
	return START_REFUSALS[message] ?? message;
}

/** The one line the box shows under a failed parse. */
export function animateErrorLine(parse: Extract<AnimateParse, { ok: false }>): string {
	return parse.token === null ? parse.message : `"${parse.token}": ${parse.message}`;
}

/** The hint line: the grammar plus an example for this control. */
export function animateHint(control: AnimateControl): string {
	return `<0..1> --over <N>beats|bars|phrases|ms [--anchor next_beat|next_downbeat|next_phrase] [--clock master|1-4]   e.g. ${ANIMATE_CONTROLS[control].example}`;
}

/** The CLI twin of an animation, so the box teaches the command line too. */
export function animateCli(deck: DeckId | null, control: AnimateControl, expr: string): string {
	const scope = ANIMATE_CONTROLS[control].perDeck ? `${deck} ` : '';
	return `opendj animate ${scope}${control} "${expr.trim().split(/\s+/).join(' ')}"`;
}

function _deck(control: AnimateControl, deck: unknown): DeckId {
	const spec = ANIMATE_CONTROLS[control];
	if (!spec.perDeck) {
		if (deck !== null && deck !== undefined) throw new RangeError(`${control} belongs to no deck; omit deck`);
		return 1;
	}
	if (deck !== 1 && deck !== 2 && deck !== 3 && deck !== 4) throw new RangeError(`${control} needs deck 1..4, got ${JSON.stringify(deck)}`);
	return deck;
}

export function isAnimateControl(value: unknown): value is AnimateControl {
	return typeof value === 'string' && Object.hasOwn(ANIMATE_CONTROLS, value);
}

/** The AGENT-03 ramp payload for one animation; throws the parse's one line on a bad expression. */
export function animateRampPayload(deck: DeckId | null, control: AnimateControl, expr: string): AnimateRampPayload {
	if (!isAnimateControl(control)) throw new TypeError(`unknown control ${JSON.stringify(control)}; expected one of ${ANIMATE_CONTROL_IDS.join(', ')}`);
	const deckId = _deck(control, deck);
	const parse = parseAnimateExpr(expr);
	if (!parse.ok) throw new SyntaxError(animateErrorLine(parse));
	return { command: ANIMATE_CONTROLS[control].command(deckId, parse.to), to: parse.to, over: parse.over };
}

/** The running-indicator key of a ramp command; null for a command no dial shows. */
export function animationKeyOf(command: Record<string, unknown>): string | null {
	const deck = command.deck as DeckId;
	if (command.type === 'eq') return `${deck}:${String(command.band)}`;
	if (command.type === 'stem_gain') return `${deck}:stem-${String(command.stem)}`;
	if (command.type === 'crossfader') return 'crossfader';
	if (command.type === 'trim' || command.type === 'filter' || command.type === 'fader' || command.type === 'tempo') {
		return `${deck}:${command.type}`;
	}
	return null;
}

const ROLE_CONTROLS: Readonly<Record<string, AnimateControl>> = {
	low: 'eq_low',
	mid: 'eq_mid',
	high: 'eq_high',
	trim: 'trim',
	filter: 'filter',
	fader: 'fader',
	'stem-vocal': 'stem_vocal',
	'stem-instrumental': 'stem_instrumental',
	'stem-drums': 'stem_drums',
	'stem-bass': 'stem_bass'
};

/** The control a right-clicked dial names by its `data-knob-id` (`1:low`, `2:fader`, `crossfader`); null for one no ramp drives (headphone dials). */
export function animateTargetOfKnobId(id: string): { deck: DeckId | null; control: AnimateControl } | null {
	if (id === 'crossfader') return { deck: null, control: 'crossfader' };
	const [deckText, role] = id.split(':');
	const deck = Number(deckText);
	if (deck !== 1 && deck !== 2 && deck !== 3 && deck !== 4) return null;
	const control = ROLE_CONTROLS[role ?? ''];
	return control === undefined ? null : { deck, control };
}
