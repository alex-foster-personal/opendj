/**
 * AutoPlay Next orchestrator (pin fc60002b81a8): the thin, stateful half of
 * the featurette. All decision math lives in auto-play-next.ts (pure,
 * fully unit-tested); this file only reads deckStates, polls on the same
 * $effect.root + setInterval shape auto-play.svelte.ts already uses, and
 * dispatches ordinary performance commands (beat_loop, loop, eq) - no new
 * engine surface, audio-engine.svelte.ts is untouched.
 *
 * PLAY-11 (issue #3532): beat_loop on a downbeat, guaranteed loop release.
 *
 * v1 hard-cut, deliberately imperfect (own file/featurette per the pin):
 * armed state is in-memory only (no persisted pref), one outgoing/incoming
 * pair at a time, and a failed precondition (no anlz, no beatgrid, no
 * follower loaded) just refuses to arm rather than degrading further.
 */
import { DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
import {
	approximateDropMs,
	AUTO_PLAY_NEXT_MAX_LOOP_MS,
	bassEntryMs,
	DEFAULT_AUTO_PLAY_NEXT_CONFIG,
	duckedLowEqKnob,
	findRepetitiveLoopWindow,
	planAutoPlayNextBeatLoop,
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
let _loopEngaged = false;
let _eqDucked = false;
let _loopingSinceMs: number | null = null;
let _releaseInFlight: Promise<void> | null = null;

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
	_loopEngaged = false;
	_eqDucked = false;
	_loopingSinceMs = null;
	autoPlayNextState.armed = false;
	autoPlayNextState.outgoing = null;
	autoPlayNextState.incoming = null;
	autoPlayNextState.phase = 'idle';
}

async function _releaseOutgoing(deck: DeckId | null, restoreEq: boolean): Promise<void> {
	if (deck === null) {
		_loopEngaged = false;
		_eqDucked = false;
		return;
	}
	if (_releaseInFlight !== null) {
		await _releaseInFlight;
		return;
	}
	_releaseInFlight = (async () => {
		if (_loopEngaged) {
			await dispatchPerformanceCommand({ type: 'loop', deck, loop: null });
			_loopEngaged = false;
		}
		if (restoreEq && _eqDucked) {
			await dispatchPerformanceCommand({ type: 'eq', deck, band: 'low', value: 0.5 });
			_eqDucked = false;
		}
	})();
	try {
		await _releaseInFlight;
	} finally {
		_releaseInFlight = null;
	}
}

async function _finishTransition(restoreEq: boolean): Promise<void> {
	const outgoing = autoPlayNextState.outgoing;
	await _releaseOutgoing(outgoing, restoreEq);
	autoPlayNextState.phase = 'done';
	_reset();
}

async function _abortTransition(toast: string | null): Promise<void> {
	const outgoing = autoPlayNextState.outgoing;
	const restoreEq = autoPlayNextState.phase === 'ducked';
	await _releaseOutgoing(outgoing, restoreEq);
	if (toast !== null) pushToast(toast, 'info');
	_reset();
}

/**
 * Arm AutoPlay Next: find the outgoing (current master) deck's last
 * repetitive 8-beat window and loop it on a downbeat via beat_loop. Refuses
 * (returns false, toasts why) rather than guessing when either deck lacks the
 * analysis this needs or no downbeat-aligned span fits.
 */
export async function armAutoPlayNext(
	config: AutoPlayNextConfig = DEFAULT_AUTO_PLAY_NEXT_CONFIG
): Promise<boolean> {
	if (autoPlayNextState.armed) return false;
	const snaps = _snaps();
	const outgoing = pickSourceDeck(snaps);
	if (outgoing === null) {
		pushToast('auto-play next: no master deck to loop', 'info');
		return false;
	}
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
	const plan = planAutoPlayNextBeatLoop(
		outgoingAnlz.beatgrid.beats,
		window,
		outgoingDuration / 1000
	);
	if (plan === null) {
		pushToast('auto-play next: no downbeat-aligned 8-beat loop window', 'info');
		return false;
	}
	try {
		await dispatchPerformanceCommand({
			type: 'beat_loop',
			deck: outgoing.id,
			beats: plan.beats,
			start_ms: plan.start_ms
		});
	} catch {
		pushToast('auto-play next: loop engage failed', 'error');
		return false;
	}
	_loopEngaged = true;
	_loopingSinceMs = Date.now();
	autoPlayNextState.armed = true;
	autoPlayNextState.outgoing = outgoing.id;
	autoPlayNextState.incoming = incoming;
	autoPlayNextState.phase = 'looping';
	_dropMs = null;
	_timer = setInterval(() => {
		void _tick(config);
	}, POLL_MS);
	return true;
}

/** Cancel AutoPlay Next: release the outgoing loop and any EQ duck. */
export async function cancelAutoPlayNext(): Promise<void> {
	if (!autoPlayNextState.armed) return;
	const restoreEq = autoPlayNextState.phase === 'ducked';
	await _finishTransition(restoreEq);
}

async function _tick(config: AutoPlayNextConfig): Promise<void> {
	const incoming = autoPlayNextState.incoming;
	const outgoing = autoPlayNextState.outgoing;
	if (incoming === null || outgoing === null) return;
	const outgoingState = deckStates[outgoing];
	if (outgoingState.stable_id === null || !outgoingState.playing) {
		await _abortTransition(null);
		return;
	}
	const incomingAnlz = deckStates[incoming].anlz;
	const incomingDuration = deckStates[incoming].duration_ms;
	if (incomingAnlz === null || incomingDuration === null) return;
	const beats = incomingAnlz.beatgrid.beats;
	const positionMs = deckStates[incoming].position_ms;

	if (autoPlayNextState.phase === 'looping') {
		if (_loopingSinceMs !== null && Date.now() - _loopingSinceMs > AUTO_PLAY_NEXT_MAX_LOOP_MS) {
			await _abortTransition('auto-play next: bass entry timeout, loop released');
			return;
		}
		const enteredMs = bassEntryMs(
			incomingAnlz.waveform,
			beats,
			incomingDuration / 1000,
			positionMs,
			config
		);
		if (enteredMs === null) return;
		await dispatchPerformanceCommand({
			type: 'eq',
			deck: outgoing,
			band: 'low',
			value: duckedLowEqKnob(config.lowEqDuckFraction)
		});
		_eqDucked = true;
		_dropMs = approximateDropMs(beats, enteredMs, config);
		if (_dropMs === null) {
			await _finishTransition(true);
			return;
		}
		autoPlayNextState.phase = 'ducked';
		return;
	}

	if (autoPlayNextState.phase === 'ducked') {
		if (_dropMs === null || positionMs >= _dropMs) {
			await _finishTransition(true);
		}
	}
}
