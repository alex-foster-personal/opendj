/**
 * AutoPlay Next orchestrator (pin fc60002b81a8): the thin, stateful half of
 * the featurette. All decision math lives in auto-play-next.ts (pure,
 * fully unit-tested); this file only reads deckStates, polls on the same
 * $effect.root + setInterval shape auto-play.svelte.ts already uses, and
 * dispatches ordinary performance commands (loop, eq) - no new engine
 * surface, audio-engine.svelte.ts is untouched.
 *
 * v1 hard-cut, deliberately imperfect (own file/featurette per the pin):
 * armed state is in-memory only (no persisted pref), one outgoing/incoming
 * pair at a time, and a failed precondition (no anlz, no beatgrid, no
 * follower loaded) just refuses to arm rather than degrading further.
 */
import { DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
import {
	approximateDropMs,
	bassEntryMs,
	DEFAULT_AUTO_PLAY_NEXT_CONFIG,
	duckedLowEqKnob,
	findRepetitiveLoopWindow,
	loopWindowToMs,
	type AutoPlayNextConfig
} from '$lib/rb/auto-play-next';
import { pickSourceDeck, type AutoPlayDeckSnap } from '$lib/rb/auto-play';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { pushToast } from '$lib/stores.svelte';
import type { DeckId } from '$lib/rb/deck-slots';

const POLL_MS = 250;

type Phase = 'idle' | 'looping' | 'ducked' | 'done';

export const autoPlayNextState = $state<{
	armed: boolean;
	outgoing: DeckId | null;
	incoming: DeckId | null;
	phase: Phase;
}>({ armed: false, outgoing: null, incoming: null, phase: 'idle' });

let _timer: ReturnType<typeof setInterval> | null = null;
let _dropMs: number | null = null;

function _snaps(): AutoPlayDeckSnap[] {
	return DECK_IDS.map((id) => {
		const d = deckStates[id];
		return {
			id,
			stable_id: d.stable_id,
			playing: d.playing,
			position_ms: d.position_ms,
			duration_ms: d.duration_ms,
			is_master: d.is_master,
			beat_sync_enabled: d.beat_sync_enabled
		};
	});
}

function _reset(): void {
	if (_timer !== null) {
		clearInterval(_timer);
		_timer = null;
	}
	_dropMs = null;
	autoPlayNextState.armed = false;
	autoPlayNextState.outgoing = null;
	autoPlayNextState.incoming = null;
	autoPlayNextState.phase = 'idle';
}

/**
 * Arm AutoPlay Next: find the outgoing (current master) deck's last
 * repetitive 8-beat window and loop it. Refuses (returns false, toasts why)
 * rather than guessing when either deck lacks the analysis this needs.
 */
export function armAutoPlayNext(config: AutoPlayNextConfig = DEFAULT_AUTO_PLAY_NEXT_CONFIG): boolean {
	if (autoPlayNextState.armed) return false;
	const snaps = _snaps();
	const outgoing = pickSourceDeck(snaps);
	if (outgoing === null) {
		pushToast('auto-play next: no master deck to loop', 'info');
		return false;
	}
	// Deliberately NOT pickFollowerDeck: that picker prefers an EMPTY deck
	// first (it exists to choose a landing spot before a load). Here the
	// incoming track must already be loaded - AutoPlay Next only shortens
	// the gap before the ordinary handoff, it does not load anything itself.
	const incomingSnap = snaps.find((s) => s.id !== outgoing.id && s.stable_id !== null) ?? null;
	if (incomingSnap === null) {
		pushToast('auto-play next: no loaded incoming deck to hand off to', 'info');
		return false;
	}
	const incoming = incomingSnap.id;
	const outgoingAnlz = deckStates[outgoing.id].anlz;
	const outgoingDuration = deckStates[outgoing.id].duration_ms;
	if (outgoingAnlz === null || outgoingDuration === null) {
		pushToast('auto-play next: outgoing deck has no analysis to loop', 'info');
		return false;
	}
	const window = findRepetitiveLoopWindow(
		outgoingAnlz.waveform,
		outgoingAnlz.beatgrid.beats,
		outgoingDuration / 1000,
		config
	);
	if (window === null) {
		pushToast('auto-play next: no repetitive 8-beat window found to loop', 'info');
		return false;
	}
	const loopMs = loopWindowToMs(outgoingAnlz.beatgrid.beats, window, outgoingDuration / 1000);
	void dispatchPerformanceCommand({ type: 'loop', deck: outgoing.id, loop: loopMs });
	autoPlayNextState.armed = true;
	autoPlayNextState.outgoing = outgoing.id;
	autoPlayNextState.incoming = incoming;
	autoPlayNextState.phase = 'looping';
	_dropMs = null;
	_timer = setInterval(() => _tick(config), POLL_MS);
	return true;
}

/** Cancel AutoPlay Next: release the outgoing loop and any EQ duck. */
export function cancelAutoPlayNext(): void {
	if (!autoPlayNextState.armed) return;
	const outgoing = autoPlayNextState.outgoing;
	if (outgoing !== null) {
		void dispatchPerformanceCommand({ type: 'loop', deck: outgoing, loop: null });
		if (autoPlayNextState.phase === 'ducked') {
			void dispatchPerformanceCommand({ type: 'eq', deck: outgoing, band: 'low', value: 0.5 });
		}
	}
	_reset();
}

function _tick(config: AutoPlayNextConfig): void {
	const incoming = autoPlayNextState.incoming;
	const outgoing = autoPlayNextState.outgoing;
	if (incoming === null || outgoing === null) return;
	const incomingAnlz = deckStates[incoming].anlz;
	const incomingDuration = deckStates[incoming].duration_ms;
	if (incomingAnlz === null || incomingDuration === null) return;
	const beats = incomingAnlz.beatgrid.beats;
	const positionMs = deckStates[incoming].position_ms;

	if (autoPlayNextState.phase === 'looping') {
		const enteredMs = bassEntryMs(incomingAnlz.waveform, beats, incomingDuration / 1000, positionMs, config);
		if (enteredMs === null) return;
		void dispatchPerformanceCommand({
			type: 'eq',
			deck: outgoing,
			band: 'low',
			value: duckedLowEqKnob(config.lowEqDuckFraction)
		});
		_dropMs = approximateDropMs(beats, enteredMs, config);
		autoPlayNextState.phase = 'ducked';
		return;
	}

	if (autoPlayNextState.phase === 'ducked' && _dropMs !== null && positionMs >= _dropMs) {
		void dispatchPerformanceCommand({ type: 'loop', deck: outgoing, loop: null });
		void dispatchPerformanceCommand({ type: 'eq', deck: outgoing, band: 'low', value: 0.5 });
		autoPlayNextState.phase = 'done';
		_reset();
	}
}
