/**
 * Digits 1-9 command selected dial/fader positions; Cmd/Ctrl+digit jumps (#9453, D2).
 * = and - double and halve the glide length, 1 to 16 beats (#9455, D4).
 *
 * the maintainer, Fri 6 Nov 2026: "pressing 5 resets it ... default is to GET to that value over
 * 4 beats - not jump to it. Maybe cmd 2 jumps to it". Position table: 1 = 0, 2 = 0.125,
 * 3 = 0.25, 4 = 0.375, 5 = 0.5 centre, 6 = 0.625, 7 = 0.75, 8 = 0.875, 9 = 1.0.
 * Faders use the same table.
 *
 * Pure: no Svelte, no DOM, no runtime import. Command descriptors match the
 * command-registry (#9394) shape for draft PR #9549; the planner module (#9452) is
 * not on main yet -- this file emits small `CommandedMove` plans until D1 lands.
 */

export type CommandedDigit = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9;
export type CommandedScope = 'dial' | 'fader';

/** the maintainer, Fri 6 Nov 2026: 1 = full left/down, 5 = centre, 9 = full right/up. Faders use the same table. */
export const COMMANDED_POSITIONS: Readonly<Record<CommandedDigit, number>> = {
	1: 0,
	2: 0.125,
	3: 0.25,
	4: 0.375,
	5: 0.5,
	6: 0.625,
	7: 0.75,
	8: 0.875,
	9: 1
};
export const DEFAULT_COMMANDED_BEATS = 4;

/** #9455 (D4): = and - double and halve the glide length along this ladder, clamped at both ends. */
export const COMMANDED_BEAT_STEPS: readonly number[] = [1, 2, 4, 8, 16];

export type CommandedDigitCommandId =
	`dial.position.${CommandedDigit}` | `dial.jump.${CommandedDigit}`;
export type CommandedDurationDirection = 'longer' | 'shorter';
export type CommandedDurationCommandId = `dial.duration.${CommandedDurationDirection}`;
export type CommandedCommandId = CommandedDigitCommandId | CommandedDurationCommandId;

export interface CommandedKeyCommand {
	id: CommandedDigitCommandId;
	label: string;
	defaultKeys: readonly string[];
	scopes: readonly CommandedScope[];
	digit: CommandedDigit;
	jump: boolean;
}

const SCOPES: readonly CommandedScope[] = ['dial', 'fader'];

function _commandFor(digit: CommandedDigit, jump: boolean): CommandedKeyCommand {
	const value = COMMANDED_POSITIONS[digit];
	const id: CommandedDigitCommandId = jump ? `dial.jump.${digit}` : `dial.position.${digit}`;
	const defaultKeys = jump ? [`Mod+${digit}`] : [`${digit}`];
	const label = jump
		? `Jump selected dial or fader to ${digit} (${value})`
		: `Glide selected dial or fader to ${digit} (${value}) over the move length`;
	return { id, label, defaultKeys, scopes: SCOPES, digit, jump };
}

const _DIGITS: CommandedDigit[] = [1, 2, 3, 4, 5, 6, 7, 8, 9];

/** The 18 commands, in the command-registry (#9394) shape, built by a loop over 1..9. */
export const COMMANDED_KEY_COMMANDS: readonly CommandedKeyCommand[] = _DIGITS.flatMap((digit) => [
	_commandFor(digit, false),
	_commandFor(digit, true)
]);

export interface CommandedDurationCommand {
	id: CommandedDurationCommandId;
	label: string;
	defaultKeys: readonly string[];
	scopes: readonly CommandedScope[];
	direction: CommandedDurationDirection;
}

/** dial.duration.longer (= or +) and dial.duration.shorter (- or _), in the command-registry shape. */
export const COMMANDED_DURATION_COMMANDS: readonly CommandedDurationCommand[] = [
	{
		id: 'dial.duration.longer',
		label: 'Double the commanded move length (1, 2, 4, 8, 16 beats)',
		defaultKeys: ['=', '+'],
		scopes: SCOPES,
		direction: 'longer'
	},
	{
		id: 'dial.duration.shorter',
		label: 'Halve the commanded move length (1, 2, 4, 8, 16 beats)',
		defaultKeys: ['-', '_'],
		scopes: SCOPES,
		direction: 'shorter'
	}
];

/** The next length on COMMANDED_BEAT_STEPS, clamped at 1 and 16; throws RangeError for a length off the ladder. */
export function stepCommandedBeats(beats: number, direction: CommandedDurationDirection): number {
	const index = COMMANDED_BEAT_STEPS.indexOf(beats);
	if (index < 0) {
		throw new RangeError(
			`commanded move length must be one of ${COMMANDED_BEAT_STEPS.join(', ')}, got ${beats}`
		);
	}
	const next = direction === 'longer' ? index + 1 : index - 1;
	return COMMANDED_BEAT_STEPS[Math.min(COMMANDED_BEAT_STEPS.length - 1, Math.max(0, next))];
}

export interface CommandedKeyEvent {
	key: string;
	metaKey: boolean;
	ctrlKey: boolean;
	altKey: boolean;
	shiftKey: boolean;
	repeat?: boolean;
	target?: EventTarget | null;
	preventDefault?: () => void;
	stopPropagation?: () => void;
}

/** '1'..'9' -> digit, anything else (including '0') -> null. */
export function commandedDigitOf(key: string): CommandedDigit | null {
	if (key.length !== 1) return null;
	const n = Number(key);
	if (!Number.isInteger(n) || n < 1 || n > 9) return null;
	return n as CommandedDigit;
}

function _isFaderId(id: string): boolean {
	return id === 'crossfader' || /^\d+:fader$/.test(id);
}

/** null for an empty selection; 'fader' when every id is a fader; else 'dial'. */
export function commandedScopeOf(ids: readonly string[]): CommandedScope | null {
	if (ids.length === 0) return null;
	if (ids.every(_isFaderId)) return 'fader';
	return 'dial';
}

/** The command a key event names, or null: Alt or Shift held, not a digit 1-9. Mod = metaKey || ctrlKey -> dial.jump.N. */
export function resolveCommandedKey(e: CommandedKeyEvent): CommandedKeyCommand | null {
	if (e.altKey || e.shiftKey) return null;
	const digit = commandedDigitOf(e.key);
	if (digit === null) return null;
	const jump = e.metaKey || e.ctrlKey;
	return _commandFor(digit, jump);
}

const _DURATION_KEYS: Readonly<Record<string, CommandedDurationDirection>> = {
	'=': 'longer',
	'+': 'longer',
	'-': 'shorter',
	_: 'shorter'
};

/** The length command a key names (=, +, -, _ with Shift allowed), or null when Mod or Alt is held. */
export function resolveCommandedDurationKey(e: CommandedKeyEvent): CommandedDurationCommand | null {
	if (e.metaKey || e.ctrlKey || e.altKey) return null;
	const direction = _DURATION_KEYS[e.key];
	if (direction === undefined) return null;
	return COMMANDED_DURATION_COMMANDS.find((c) => c.direction === direction) ?? null;
}

export interface CommandedMove {
	controlId: string;
	from: number;
	to: number;
	beats: number;
}

/** One move per selected id whose readValue is not null, in selection order; beats = 0 when jump. */
export function planCommandedMoves(input: {
	command: CommandedKeyCommand;
	selectedIds: readonly string[];
	readValue: (id: string) => number | null;
	beats?: number;
}): CommandedMove[] {
	const glideBeats = input.beats ?? DEFAULT_COMMANDED_BEATS;
	const beats = input.command.jump ? 0 : glideBeats;
	const to = COMMANDED_POSITIONS[input.command.digit];
	const moves: CommandedMove[] = [];
	for (const controlId of input.selectedIds) {
		const from = input.readValue(controlId);
		if (from === null) continue;
		moves.push({ controlId, from, to, beats });
	}
	return moves;
}

/** The ANIM-01 expression for a glide, e.g. '0.5 --over 4beats'. Throws RangeError for beats <= 0 or non-integer beats. */
export function commandedRampExpr(move: CommandedMove): string {
	if (!(move.beats > 0) || !Number.isInteger(move.beats)) {
		throw new RangeError(`commanded glide needs a whole number of beats > 0, got ${move.beats}`);
	}
	return `${move.to} --over ${move.beats}beats`;
}

export interface CommandedKeyDeps {
	selectedIds: () => readonly string[];
	readValue: (id: string) => number | null;
	setAbsolute: (id: string, value: number) => void;
	startGlides: (moves: readonly CommandedMove[]) => void;
	isTyping: (target: EventTarget | null | undefined) => boolean;
	settingsOpen: () => boolean;
	beats?: () => number;
	/** #9455: store the commanded move length; dial.duration.* throws without it. */
	setBeats?: (beats: number) => void;
}

export type CommandedRegistryCommand = CommandedKeyCommand | CommandedDurationCommand;

/**
 * Keydown handler body. Returns the planned moves when it CLAIMED the key (an empty list for
 * a length key), null otherwise.
 */
export function handleCommandedKeydown(
	e: CommandedKeyEvent,
	deps: CommandedKeyDeps,
	run?: (command: CommandedRegistryCommand) => void
): CommandedMove[] | null {
	if (deps.isTyping(e.target ?? null) || deps.settingsOpen()) return null;
	const duration = resolveCommandedDurationKey(e);
	if (duration !== null) return _handleDurationKey(e, duration, deps, run);
	const command = resolveCommandedKey(e);
	if (command === null) return null;
	const moves = _planFor(command, deps);
	if (moves.length === 0) return null;
	e.preventDefault?.();
	e.stopPropagation?.();
	if (e.repeat) return moves;
	if (run !== undefined) {
		run(command);
	} else {
		_applyMoves(command, moves, deps);
	}
	return moves;
}

function _handleDurationKey(
	e: CommandedKeyEvent,
	command: CommandedDurationCommand,
	deps: CommandedKeyDeps,
	run?: (command: CommandedRegistryCommand) => void
): CommandedMove[] | null {
	if (!deps.selectedIds().some((id) => deps.readValue(id) !== null)) return null;
	e.preventDefault?.();
	e.stopPropagation?.();
	if (e.repeat) return [];
	if (run !== undefined) {
		run(command);
	} else {
		runCommandedDurationCommand(command, deps);
	}
	return [];
}

/** Step the stored commanded move length one rung along COMMANDED_BEAT_STEPS; returns the new length. */
export function runCommandedDurationCommand(
	command: CommandedDurationCommand,
	deps: CommandedKeyDeps
): number {
	if (deps.setBeats === undefined) {
		throw new Error(`${command.id}: the dial or fader host cannot store a move length`);
	}
	const next = stepCommandedBeats(deps.beats?.() ?? DEFAULT_COMMANDED_BEATS, command.direction);
	deps.setBeats(next);
	return next;
}

function _planFor(command: CommandedKeyCommand, deps: CommandedKeyDeps): CommandedMove[] {
	return planCommandedMoves({
		command,
		selectedIds: deps.selectedIds(),
		readValue: deps.readValue,
		beats: deps.beats?.() ?? DEFAULT_COMMANDED_BEATS
	});
}

function _applyMoves(
	command: CommandedKeyCommand,
	moves: readonly CommandedMove[],
	deps: CommandedKeyDeps
): void {
	if (command.jump) {
		for (const move of moves) deps.setAbsolute(move.controlId, move.to);
	} else {
		deps.startGlides(moves);
	}
}

/** Run one commanded command on the current selection (command registry path, no key event). */
export function runCommandedCommand(
	command: CommandedKeyCommand,
	deps: CommandedKeyDeps
): CommandedMove[] {
	const moves = _planFor(command, deps);
	_applyMoves(command, moves, deps);
	return moves;
}
