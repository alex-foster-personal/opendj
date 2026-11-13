/**
 * Deck-state effects for all four decks (issue #10282).
 *
 * The future replica-desk layout unmounts Deck panels while the engine keeps
 * running; behaviours that lived only inside Deck.svelte would stop. This
 * module runs them once per deck via a shared `$effect.root`, started from
 * `routes/performance/+page.svelte` (the deck engine exists only on that route).
 *
 * Panel presentation (ResizeObserver, pause-dim timers) stays in Deck.svelte.
 */
import { getDeckState, DECK_IDS } from '$lib/rb/audio-engine.svelte';
import { bannerSyncError, noteDeckError } from '$lib/rb/deck-error-id.svelte';
import { deckBannerDismissMs } from '$lib/rb/deck-banner-policy';
import { presentationTickFailure } from '$lib/rb/presentation-tick-failure.svelte';
import {
	performanceCommandStatus,
	dismissPerformanceDeckError,
	runPerformanceCommandFromUi
} from '$lib/rb/performance-ipc.svelte';
import { loadLyrics } from '$lib/lyrics/lyrics-cache.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import {
	loadedStemSeparation,
	stemSeparation,
	stemSeparationLoadError,
	type StemSeparationPort
} from '$lib/rb/stem-separation-port';
import { stemStatusView } from '$lib/rb/stem-status';
import { subscribeLoadedDeckStemsRefresh } from '$lib/rb/loaded-deck-stems-refresh';
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import { rustCommandUnsupported } from '$lib/audio-engine/rust-mode.svelte';
import { SeparateEndProbe, lookForBundle } from '$lib/rb/stem-separate-probe';
import type { DeckId } from '$lib/rb/deck-slots';

/** The text the deck's error banner shows, or null (DECKGAP-7691c: an unsynced start's reason glows on Play, not here). */
export function deckControlError(deckId: DeckId): string | null {
	const deck = getDeckState(deckId);
	return (
		performanceCommandStatus.deck_errors[deckId] ??
		bannerSyncError(deck.sync_error) ??
		deck.processor_error ??
		presentationTickFailure()
	);
}

/** STEM-77: the loaded track while the deck shows NO STEMS, else null. */
export function deckSeparateId(deckId: DeckId): string | null {
	const deck = getDeckState(deckId);
	return deck.stable_id !== null && stemStatusView(deck.stems).name === 'none'
		? deck.stable_id
		: null;
}

type DeckSeparationSlot = {
	port: StemSeparationPort | null;
	loadError: string | null;
	probeAnswers: number;
};

const _emptySeparationSlot = (): DeckSeparationSlot => ({
	port: null,
	loadError: null,
	probeAnswers: 0
});

const _separation = $state<Record<DeckId, DeckSeparationSlot>>({
	1: _emptySeparationSlot(),
	2: _emptySeparationSlot(),
	3: _emptySeparationSlot(),
	4: _emptySeparationSlot()
});

export function deckSeparation(deckId: DeckId): DeckSeparationSlot {
	return _separation[deckId];
}

const _stemsSettledWaiters: Record<DeckId, Array<() => void>> = { 1: [], 2: [], 3: [], 4: [] };

function _stemsSettled(deckId: DeckId): Promise<void> {
	if (getDeckState(deckId).stems.status !== 'loading') return Promise.resolve();
	return new Promise((resolve) => _stemsSettledWaiters[deckId].push(resolve));
}

function _newProbe(deckId: DeckId): SeparateEndProbe {
	// Sol 4291048002: `stem_load` loads whatever the deck holds NOW, so it is an
	// answer for `stableId` only while the deck still holds that track.
	// The stem_load returns once the deck has started reloading, so the look
	// waits for that load to settle before it answers (Codex on #6819).
	// Only a stem_load that RAN is an answer: one refused (a preset lock, no
	// session) leaves the probe failed and the button offers to look again.
	return new SeparateEndProbe(
		async (stableId) =>
			deckSeparateId(deckId) === stableId &&
			lookForBundle({
				run: async () =>
					(
						await runPerformanceCommandFromUi({
							type: 'stem_load',
							deck: deckId,
							stable_id: stableId
						})
					).ok,
				settled: () => _stemsSettled(deckId),
				holds: () => getDeckState(deckId).stable_id === stableId
			}),
		() => {
			_separation[deckId].probeAnswers += 1;
		}
	);
}

const _separateProbes: Record<DeckId, SeparateEndProbe> = {
	1: _newProbe(1),
	2: _newProbe(2),
	3: _newProbe(3),
	4: _newProbe(4)
};

export function deckSeparateProbe(deckId: DeckId): SeparateEndProbe {
	return _separateProbes[deckId];
}

let _stopRoot: (() => void) | null = null;
let _starts = 0;

/** Start every deck-state effect for all four decks (issue #10282). Idempotent: a second start
 * shares the running root rather than adding a second set; each returned stop releases its own
 * start once, and the last one tears every effect and subscription down. */
export function startDeckEffects(): () => void {
	_starts += 1;
	if (_stopRoot === null) {
		// claimPerformanceMount allows a remount to start before the old page's teardown runs;
		// the refcount lets the old stop release without killing the new mount's effects.
		_stopRoot = $effect.root(() => {
			for (const deckId of DECK_IDS) _deckEffects(deckId);
		});
	}
	let released = false;
	return () => {
		if (released) return;
		released = true;
		_starts -= 1;
		if (_starts === 0 && _stopRoot !== null) {
			_stopRoot();
			_stopRoot = null;
		}
	};
}

function _deckEffects(deckId: DeckId): void {
	const controlError = $derived(deckControlError(deckId));
	// DECKUX-LABEL-02: an expected, self-resolving condition (stems still
	// loading) clears itself and is not painted as a fault; a real fault stays
	// until dismissed. Primitives, so the timer restarts only on a real change.
	const controlErrorDismissMs = $derived(
		controlError === null
			? null
			: deckBannerDismissMs(controlError, getDeckState(deckId).stems.status)
	);
	const separateId = $derived(deckSeparateId(deckId));

	/**
	 * Deck-error logging: mint and log the banner's id whenever what the banner shows changes.
	 *
	 * An $effect rather than a $derived because this WRITES a log row, and a
	 * derivation that logs would fire on Svelte's own re-evaluation schedule
	 * rather than on real changes. `noteDeckError` is idempotent for an
	 * unchanged message, so the re-runs this effect takes from the three
	 * unrelated deck-state sources behind `controlError` cost nothing.
	 *
	 * Covers all three sources uniformly. That matters because only two of them
	 * (`_persistCommandError`, `_recordProcessorFailure`) raise a toast; the
	 * Beat Sync paths set `sync_error` directly and had no id anywhere at all.
	 */
	$effect(() => {
		noteDeckError(deckId, controlError);
	});

	// Banner auto-dismiss (DECKUX-LABEL-02): an expected banner clears itself.
	$effect(() => {
		const ms = controlErrorDismissMs;
		if (ms === null) return;
		const timer = setTimeout(() => dismissPerformanceDeckError(deckId), ms);
		return () => clearTimeout(timer);
	});

	// Lyrics: warm the loaded track's lyrics while the deck lyric line is on.
	$effect(() => {
		const stableId = getDeckState(deckId).stable_id;
		if (!uiPrefs.lyrics_deck_line || !uiPrefs.lyrics_global || stableId === null) return;
		void loadLyrics(stableId);
	});

	// STEM-9934: a NO STEMS deck looks for its bundle again on a covering job, a library change or a resync.
	$effect(() => {
		return subscribeLoadedDeckStemsRefresh({
			deck: deckId,
			state: () => getDeckState(deckId),
			subscribeKind,
			subscribeResync,
			canLoadStems: () => !rustCommandUnsupported('stem_load'),
			loadStems: (stable_id) =>
				runPerformanceCommandFromUi({ type: 'stem_load', deck: deckId, stable_id })
		});
	});

	// STEM-77: load the separation store's chunk the first time this deck shows NO STEMS.
	$effect(() => {
		const slot = _separation[deckId];
		if (separateId === null || slot.port !== null || slot.loadError !== null) return;
		const loaded = loadedStemSeparation();
		if (loaded !== null) {
			slot.port = loaded;
			return;
		}
		stemSeparation().then(
			(port) => {
				slot.port = port;
			},
			(exc: unknown) => {
				slot.loadError = stemSeparationLoadError() ?? String(exc);
			}
		);
	});

	// STEM-77: keep the separation source check fresh while this deck shows NO STEMS.
	$effect(() => {
		if (separateId === null) return;
		const slot = _separation[deckId];
		const store = slot.port;
		if (store === null) return;
		store.ensureChecked();
		const timer = setInterval(() => store.ensureChecked(), 15_000);
		return () => clearInterval(timer);
	});

	// Stems-settled waiters: release every look waiting on this deck's stem load once it settles.
	$effect(() => {
		if (
			getDeckState(deckId).stems.status === 'loading' ||
			_stemsSettledWaiters[deckId].length === 0
		)
			return;
		const waiters = _stemsSettledWaiters[deckId];
		_stemsSettledWaiters[deckId] = [];
		for (const resolve of waiters) resolve();
	});

	// STEM-77: note NO STEMS and probe for the bundle once a covering job ends.
	$effect(() => {
		if (separateId === null) return;
		const separation = _separation[deckId].port;
		if (separation === null) return;
		const separateProbe = _separateProbes[deckId];
		separation.noteNoStems(separateId);
		void separateProbe.observe(separateId, separation.job(separateId));
	});
}
