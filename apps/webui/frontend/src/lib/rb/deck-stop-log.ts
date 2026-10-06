/**
 * PLAY-18 (CORE, Tue 6 Oct 2026): every deck STOP is logged with its cause.
 *
 * Soak round 4 found a 05:19:57-05:21:06Z stop with zero log evidence: stops
 * were unattributed. Now each playing-to-stopped edge writes ONE structured
 * line (`[perf-event] deck-stop deck=N: cause=... seq=... position_ms=...
 * stable_id=...`) and becomes the deck's `last_stop` in the UI mirror, which
 * the engine logs on ingest so the record survives the tab.
 *
 * Causes:
 * - `user-ui`: a person pressed something (`runPerformanceCommandFromUi`).
 * - `agent-command`: the AGENT-03/19 order bus or the IPC bridge.
 * - `autoplay-handoff`: a command AutoPlay dispatched.
 * - `app-command`: any other in-app dispatch (restore, preset, rescue).
 * - `end-of-track`: the track played out.
 * - `engine`: the audio graph, a worklet, or the silence-dropout act stopped it.
 * - `reload`: the engine was disposed with the deck playing (route teardown, reload).
 * - `unattributed`: none of the above could be shown; never guessed.
 */
import { recordPerfEvent } from '$lib/rb/perf-event-log';
import type { PauseOrigin } from '$lib/rb/unexpected-pause';

/** deck-slots' DeckId, spelled as the literal perf-event-log already uses. */
type DeckId = 1 | 2 | 3 | 4;

/**
 * The stable stop-cause vocabulary (PLAY-18). Other lanes (the AutoPlay
 * handoff safety net) import these names; add to the end, never rename.
 * Mirrored by DECK_STOP_CAUSES in apps/webui/server/routes/state.py.
 */
export const DECK_STOP_CAUSES = [
	'user-ui',
	'agent-command',
	'autoplay-handoff',
	'app-command',
	'end-of-track',
	'engine',
	'reload',
	'unattributed'
] as const;

export type DeckStopCause = (typeof DECK_STOP_CAUSES)[number];

/** Who a command came from; a subset of the causes. */
export type CommandSource = Extract<DeckStopCause, 'user-ui' | 'agent-command' | 'autoplay-handoff' | 'app-command' | 'engine'>;

/** Expected stops log at info; only a stop nobody asked for warns. */
export const DECK_STOP_WARN_CAUSES: ReadonlySet<DeckStopCause> = new Set(['unattributed', 'engine', 'reload']);

export function deckStopSeverity(cause: DeckStopCause): 'info' | 'warn' {
	return DECK_STOP_WARN_CAUSES.has(cause) ? 'warn' : 'info';
}

export interface DeckStop {
	/** Per-deck count of stops this page has seen; a gap means a missed record. */
	seq: number;
	cause: DeckStopCause;
	/** True exactly when a person stopped it (cause `user-ui`). */
	user_pause: boolean;
	position_ms: number;
	stable_id: string | null;
	at: string;
}

const DECKS: readonly DeckId[] = [1, 2, 3, 4];

const _commandSource: Record<DeckId, CommandSource | null> = { 1: null, 2: null, 3: null, 4: null };
const _lastStop: Record<DeckId, DeckStop | null> = { 1: null, 2: null, 3: null, 4: null };
const _seq: Record<DeckId, number> = { 1: 0, 2: 0, 3: 0, 4: 0 };

/**
 * Attribute any stop on `decks` to `source` while `run` executes. Commands on
 * one deck are serialized by the IPC's scope queue, so a per-deck register is
 * exact; the previous value is restored for nesting.
 */
export async function withDeckCommandSource<T>(
	decks: readonly DeckId[],
	source: CommandSource,
	run: () => Promise<T>
): Promise<T> {
	const previous = decks.map((deck) => _commandSource[deck]);
	for (const deck of decks) _commandSource[deck] = source;
	try {
		return await run();
	} finally {
		decks.forEach((deck, index) => {
			_commandSource[deck] = previous[index];
		});
	}
}

/** Every deck, for commands that carry none (rescue_stop_all and friends). */
export function allDecks(): readonly DeckId[] {
	return DECKS;
}

/**
 * The cause of a playing-to-stopped edge. The engine's own origins win, so a
 * track ending while an unrelated command runs on its deck is still
 * `end-of-track`; otherwise the running command's source; otherwise
 * `unattributed`, never a guess.
 */
export function deckStopCause(deck: DeckId, origin: PauseOrigin): DeckStopCause {
	if (origin === 'natural-end') return 'end-of-track';
	if (origin === 'worklet' || origin === 'dropout') return 'engine';
	return _commandSource[deck] ?? 'unattributed';
}

/** Log one stop and make it the deck's `last_stop`. */
export function recordDeckStop(input: {
	deck: DeckId;
	cause: DeckStopCause;
	position_ms: number;
	stable_id: string | null;
}): DeckStop {
	_seq[input.deck] += 1;
	const stop: DeckStop = {
		seq: _seq[input.deck],
		cause: input.cause,
		user_pause: input.cause === 'user-ui',
		position_ms: Math.round(input.position_ms),
		stable_id: input.stable_id,
		at: new Date().toISOString()
	};
	_lastStop[input.deck] = stop;
	recordPerfEvent(
		'deck-stop',
		`cause=${stop.cause} seq=${stop.seq} position_ms=${stop.position_ms} stable_id=${stop.stable_id ?? 'none'}`,
		input.deck,
		deckStopSeverity(stop.cause)
	);
	return stop;
}

export function readLastDeckStop(deck: DeckId): DeckStop | null {
	return _lastStop[deck];
}

export function resetDeckStopLogForTest(): void {
	for (const deck of DECKS) {
		_commandSource[deck] = null;
		_lastStop[deck] = null;
		_seq[deck] = 0;
	}
}
