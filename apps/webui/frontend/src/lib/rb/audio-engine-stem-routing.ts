/**
 * Lazy stem routing helpers split from audio-engine.svelte.ts (issue #8631, parent #8581).
 * Leaf module: never import audio-engine.svelte.ts here (frontend.import_cycles slack is 0).
 */

import { fetchStemAudioArrayBuffers, STEM_LAYOUT_PART_NAMES } from '$lib/rb/api-rb';
import { assertDeckReplacementAllowed } from '$lib/rb/audio-engine-guards';
import { _controlSegmentAt, type _DeckProcessor, type _DeckRuntime } from '$lib/rb/deck-runtime';
import { recordPerfEvent, recordPerfTiming, stageTimer } from '$lib/rb/perf-event-log';
import {
	awaitStemArtifact,
	engageStemsOnDeck,
	landHeldStemsOrSettle,
	landStemsOnDeck,
	stemBlockCheck,
	stemsBlockedState,
	STEM_HELD_BY_PRESSURE,
	STEM_HELD_BY_TRANSPORT,
	type StemHandoffSegment,
	type StemLandingOutcome,
	type StemLandingPort,
	type StemLandingProcessor
} from '$lib/rb/stem-hydrate-wait';
import {
	AlignedStemDeckProcessor,
	conformStemBuffersToFrameCount,
	decodeStemBuffers,
	type StemBuffers,
	loadingStemDeckState,
	readyStemDeckState,
	unavailableStemDeckState
} from '$lib/rb/stem-graph';
import { truncateAtOutPoint } from '$lib/rb/edit-window-buffer';
import { deckPlaysOriginalFile, STEM_ENGAGE_CROSSFADE_SEC } from '$lib/rb/stem-neutral-bypass';
import type { StemDeckState, StemFetchProgress, StemLoadPhase } from '$lib/rb/stem-types';
import { applyStemCarry } from '$lib/rb/track-load-reset';
import type { DeckId } from '$lib/player/constants';
import { stretchScheduleChange } from '$lib/player/transport/schedule-math';
import { pushToast } from '$lib/stores.svelte';
import { deckStates } from '$lib/player/state.svelte';

let _rt: { readonly [deck in DeckId]: _DeckRuntime };
let _ctx: AudioContext | null = null;
let _retireProcessor: (processor: _DeckProcessor) => void;
let _recordProcessorFailure: (deck: DeckId, error: unknown) => void;
let _transportLeadSec: (deck: DeckId) => number;
let _commitPendingIfDue: (deck: DeckId) => void;
let _reanchorRampPending: (rt: _DeckRuntime) => boolean;
let _launchArmed: (deck: DeckId) => boolean;

/** Keep `_ctx` in the sibling aligned with the engine's graph context (stale checks use `ctx !== _ctx`). */
export function setStemRoutingEngineContext(ctx: AudioContext | null): void {
	_ctx = ctx;
}

export function bindAudioEngineStemRouting(input: {
	deckRuntime: { readonly [deck in DeckId]: _DeckRuntime };
	getContext: () => AudioContext | null;
	retireProcessor: (processor: _DeckProcessor) => void;
	recordProcessorFailure: (deck: DeckId, error: unknown) => void;
	transportLeadSec: (deck: DeckId) => number;
	commitPendingIfDue: (deck: DeckId) => void;
	reanchorRampPending: (rt: _DeckRuntime) => boolean;
	launchArmed: (deck: DeckId) => boolean;
}): void {
	_rt = input.deckRuntime;
	_ctx = input.getContext();
	_retireProcessor = input.retireProcessor;
	_recordProcessorFailure = input.recordProcessorFailure;
	_transportLeadSec = input.transportLeadSec;
	_commitPendingIfDue = input.commitPendingIfDue;
	_reanchorRampPending = input.reanchorRampPending;
	_launchArmed = input.launchArmed;
}

async function _withDeckSwap<T>(rt: _DeckRuntime, swap: () => Promise<T>): Promise<T> {
	const predecessor = rt.swapTail;
	let release!: () => void;
	rt.swapTail = new Promise<void>((resolve) => {
		release = resolve;
	});
	await predecessor;
	try {
		return await swap();
	} finally {
		release();
	}
}

/** LAZY-STEMS. True when the deck can have its processor replaced right now.
 * Same predicate the engine has always enforced for load/unload, asked as a
 * question instead of thrown as an assertion, because a background upgrade
 * arriving mid-set is an EXPECTED state to wait out, not a fault. */
function _deckIsReplaceable(deck: DeckId): boolean {
	const st = deckStates[deck];
	const rt = _rt[deck];
	try {
		assertDeckReplacementAllowed(deck, {
			playing: st.playing,
			audible: st.audible,
			transportPending: st.transport_pending,
			controlActive: rt.controlActive,
			pendingScheduleCount: rt.pending.length,
			scheduleIntentCount: rt.scheduleIntentCount
		});
		return true;
	} catch {
		return false;
	}
}

/**
 * LAZY-STEMS. Put a prepared AlignedStemDeckProcessor in place of the deck's
 * mix processor. The caller MUST have established that the deck is replaceable.
 *
 * Playhead safety: a stopped deck's position lives in `st.position_ms` (what
 * `play` resumes from) and `rt.presentation`. This function touches NEITHER, so
 * a deck cued to 1:30 is still at 1:30 after the swap. That is the whole reason
 * it is not written in terms of the load-time swap, which deliberately zeroes
 * both because it is publishing a DIFFERENT track.
 */
function _adoptStemProcessor(
	deck: DeckId,
	upgrade: { processor: AlignedStemDeckProcessor; state: StemDeckState }
): void {
	const st = deckStates[deck];
	const rt = _rt[deck];
	if (rt.nodes === null) throw new Error(`stem upgrade: deck ${deck} audio graph is missing`);
	const retired = rt.processor;
	upgrade.processor.connect(rt.nodes.analyser);
	rt.processor = upgrade.processor;
	st.stems = upgrade.state;
	if (retired !== null) _retireProcessor(retired);
}

/** LAZY-STEMS. Detach and silence a held stem upgrade that will never land
 * (deck unloaded, engine disposed, track swapped). Without this the prepared
 * worklet nodes stay connected to a context nobody owns any more: the exact
 * "PCM should drop to 0 on unload" property the perf log treats as the leak
 * signature. Safe to call when nothing is pending. */
export function _releasePendingStemUpgrade(rt: _DeckRuntime): void {
	const held = rt.heldNeutralStems;
	rt.heldNeutralStems = null;
	if (held !== null) _retireProcessor(held.processor);
	const pending = rt.pendingStemUpgrade;
	if (pending === null) return;
	rt.pendingStemUpgrade = null;
	_retireProcessor(pending.processor);
}

/** AUDFID-02: keep a ready bundle with neutral controls built but silent. The
 * deck reads stems `ready` (buttons live) while the mix processor plays the
 * original file. */
function _holdNeutralStems(
	deck: DeckId,
	upgrade: NonNullable<_DeckRuntime['heldNeutralStems']>
): void {
	const rt = _rt[deck];
	if (rt.heldNeutralStems !== null) _retireProcessor(rt.heldNeutralStems.processor);
	rt.heldNeutralStems = upgrade;
	deckStates[deck].stems = { ...upgrade.state, playback: 'original' };
	recordPerfEvent(
		'stem-neutral-bypass',
		`deck ${deck} stems ready, playing the original file until a stem control is touched`,
		deck
	);
}

/** AUDFID-02: the first stem-control touch makes the held stems audible: a
 * crossfaded live handoff on a playing deck, the stopped swap at rest. The
 * touched value is already on the held processor, so it is heard at once. */
export function _engageHeldNeutralStems(deck: DeckId): void {
	const rt = _rt[deck];
	const held = rt.heldNeutralStems;
	const ctx = _ctx;
	if (held === null) return;
	if (ctx === null) throw new Error(`deck ${deck} stems: no audio context to engage them on`);
	rt.heldNeutralStems = null;
	const st = deckStates[deck];
	const state: StemDeckState = { ...held.state, controls: st.stems.controls, playback: 'stems' };
	// prettier-ignore
	const stale = (): boolean => held.token !== rt.loadToken || ctx !== _ctx || stemsBlockedState() !== null;
	void engageStemsOnDeck(
		// prettier-ignore
		_stemUpgradeLandingPort(deck, { token: held.token, processor: held.processor, state }, ctx, stale, {
			deferBundle: false
		})
	).then(
		(outcome) => {
			if (outcome === 'busy' && !stale() && rt.processor !== held.processor) {
				rt.heldNeutralStems = held;
				return;
			}
			if (outcome === 'stale') return;
			recordPerfEvent('stem-engage', `deck ${deck} stems engaged on first touch: ${outcome}`, deck);
		},
		(error: unknown) => {
			if (rt.processor !== held.processor) _retireProcessor(held.processor);
			if (stale()) return;
			const message = error instanceof Error ? error.message : String(error);
			st.stems = { ...unavailableStemDeckState(message), status: 'error' };
			pushToast(`Deck ${deck} stems could not engage - ${message}`, 'error');
		}
	);
}

/** LAZY-STEMS / STEM-4756. Called by every presentation frame (`_tick`), to land a stem
 * upgrade that finished while the deck was playing on the first frame the deck is at rest. No-op when nothing is pending. */
export function _drainPendingStemUpgrade(deck: DeckId): void {
	const rt = _rt[deck];
	const pending = rt.pendingStemUpgrade;
	if (pending === null) return;
	const blockedState = stemsBlockedState(); // PERFMODE-15: nothing held from before a stem block may land either
	if (pending.token !== rt.loadToken || blockedState !== null) {
		// The deck moved on to another track while these stems were decoding.
		rt.pendingStemUpgrade = null;
		_retireProcessor(pending.processor);
		if (blockedState !== null && pending.token === rt.loadToken)
			deckStates[deck].stems = blockedState;
		return;
	}
	if (!_deckIsReplaceable(deck)) return;
	rt.pendingStemUpgrade = null;
	_adoptStemProcessor(deck, pending);
}

function _stemUpgradeLandingPort(
	deck: DeckId,
	upgrade: NonNullable<_DeckRuntime['pendingStemUpgrade']>,
	ctx: AudioContext,
	stale: () => boolean,
	options: { deferBundle: boolean }
): StemLandingPort {
	const rt = _rt[deck];
	const st = deckStates[deck];
	const deferImpl = () => {
		rt.pendingStemUpgrade = upgrade;
		st.stems = loadingStemDeckState({
			phase: 'waiting',
			progress: null,
			reason: STEM_HELD_BY_TRANSPORT
		});
		_drainPendingStemUpgrade(deck);
	};
	return {
		runtime: rt,
		incoming: upgrade.processor,
		clock: ctx,
		stale,
		leadSec: _transportLeadSec(deck),
		serialized: <T>(run: () => Promise<T>) => _withDeckSwap(rt, run),
		commitDue: () => _commitPendingIfDue(deck),
		rampPending: () => _reanchorRampPending(rt),
		launchArmed: () => _launchArmed(deck),
		controlSegmentAt: (when) => _controlSegmentAt(rt, when),
		startChange: (seg: StemHandoffSegment) =>
			stretchScheduleChange(
				seg.positionSec,
				true,
				seg.tempoRatio,
				seg.masterTempoEnabled,
				seg.keyShiftSemitones,
				seg.loop
			),
		retire: (processor: StemLandingProcessor) => _retireProcessor(processor as _DeckProcessor),
		commit: (when: number) => {
			rt.processor = upgrade.processor;
			st.stems = upgrade.state;
			recordPerfEvent(
				'stem-live-handoff',
				`deck ${deck} stems took over at ctx ${when.toFixed(3)}s without stopping`,
				deck
			);
		},
		defer: options.deferBundle
			? deferImpl
			: () => {
					throw new Error('stem engage: defer is disabled on this landing port');
				},
		replaceable: () => _deckIsReplaceable(deck),
		adoptStopped: () => _adoptStemProcessor(deck, upgrade),
		crossfadeSec: STEM_ENGAGE_CROSSFADE_SEC,
		createGain: () => ctx.createGain()
	};
}

/** STEM-47. Land a built stem processor: the stopped swap on a deck at rest, a
 * live handoff on a playing one (ordering and refusals: stem-live-handoff.ts).
 * `deferred` keeps the bundle for the next stop and says so on the deck. */
function _landStems(
	deck: DeckId,
	upgrade: NonNullable<_DeckRuntime['pendingStemUpgrade']>,
	ctx: AudioContext,
	stale: () => boolean
): Promise<StemLandingOutcome> {
	const rt = _rt[deck];
	const st = deckStates[deck];
	rt.pendingStemUpgrade = null;
	if (!stale())
		st.stems = loadingStemDeckState({ phase: 'switching', progress: null, reason: null });
	const port = _stemUpgradeLandingPort(deck, upgrade, ctx, stale, { deferBundle: true });
	// Inline the landing port object here (not only via _stemUpgradeLandingPort) so
	// deck-lazy-stems.test.mjs can structurally assert the handoff wiring. Reuse `port.adoptStopped`
	// so _adoptStemProcessor is not a direct call site in this function (trackify-no-stems PERFMODE-15).
	// prettier-ignore
	return landStemsOnDeck({
		runtime: rt, incoming: upgrade.processor, clock: ctx, stale,
		leadSec: _transportLeadSec(deck),
		serialized: (run) => _withDeckSwap(rt, run),
		commitDue: () => _commitPendingIfDue(deck),
		rampPending: () => _reanchorRampPending(rt),
		launchArmed: () => _launchArmed(deck),
		controlSegmentAt: (when) => _controlSegmentAt(rt, when),
		startChange: (seg) => stretchScheduleChange(seg.positionSec, true, seg.tempoRatio, seg.masterTempoEnabled, seg.keyShiftSemitones, seg.loop),
		retire: (processor) => _retireProcessor(processor as _DeckProcessor),
		commit: (when) => {
			rt.processor = upgrade.processor;
			st.stems = upgrade.state;
			recordPerfEvent(
				'stem-live-handoff',
				`deck ${deck} stems took over at ctx ${when.toFixed(3)}s without stopping`,
				deck
			);
		},
		defer: () => {
			rt.pendingStemUpgrade = upgrade;
			st.stems = loadingStemDeckState({ phase: 'waiting', progress: null, reason: STEM_HELD_BY_TRANSPORT });
			_drainPendingStemUpgrade(deck); // the deck may have come to rest during the last attempt
		},
		replaceable: () => _deckIsReplaceable(deck),
		adoptStopped: port.adoptStopped,
		crossfadeSec: STEM_ENGAGE_CROSSFADE_SEC,
		createGain: () => ctx.createGain()
	});
}

/** STEM-47. `LOAD NOW` on a bundle held for the deck's next stop. `_landStems`
 * clears the held reference and shows `switching` before anything is awaited,
 * so a rejected landing must settle the deck itself, as the upgrade does: the
 * bundle is retired (unless the deck already points at it) and the deck reads
 * a retryable error instead of `switching` until the track is reloaded. */
export function _landHeldStems(
	deck: DeckId,
	held: NonNullable<_DeckRuntime['pendingStemUpgrade']>,
	ctx: AudioContext
): Promise<void> {
	const rt = _rt[deck];
	const stale = () => held.token !== rt.loadToken || stemsBlockedState() !== null;
	return landHeldStemsOrSettle({
		land: () => _landStems(deck, held, ctx, stale),
		adopted: () => rt.processor === held.processor,
		retire: () => _retireProcessor(held.processor),
		stale: () => held.token !== rt.loadToken,
		// Stems switched off mid-landing (PERFMODE-15) settle to the block's own state.
		fail: (message) => {
			deckStates[deck].stems = stemsBlockedState() ?? {
				...unavailableStemDeckState(message),
				status: 'error'
			};
		}
	});
}

/**
 * LAZY-STEMS. The whole secondary load: probe, fetch, decode, build, swap.
 * Runs AFTER the deck is playable and is never awaited by `load`.
 *
 * Fail-fast: this never leaves the deck claiming a capability it does not have.
 * Every exit either settles `st.stems` to `ready` (processor actually in the
 * graph), `unavailable` (no bundle - a settled answer), or `error` (the bundle
 * exists but could not be used, with the reason). A deck that ends up anything
 * other than `ready` still plays; only the stem CONTROLS are absent, and
 * `_setStemControl` already refuses loudly for any non-`ready` status.
 */
export async function _upgradeDeckStems(
	deck: DeckId,
	stableId: string,
	token: number,
	ctx: AudioContext,
	mixBuffer: AudioBuffer
): Promise<void> {
	const rt = _rt[deck],
		mixAtStart = rt.processor; // STEM-4806: the processor these stems replace. A processor failure clears the deck WITHOUT bumping the token (a load in flight must still land), so the token alone let stems land on a deck with no track.
	const st = deckStates[deck];
	const t0 = performance.now();
	const stages: Record<string, number> = {};
	const time = stageTimer(stages);
	// PERFMODE-15: a mode with stems off (Trackify) settles the deck `unavailable` and ends the upgrade, now or mid-probe.
	const _stemDecodeBlocked = stemBlockCheck(st, () => token === rt.loadToken);
	if (_stemDecodeBlocked()) return;
	// prettier-ignore
	const stale = (): boolean => token !== rt.loadToken || rt.processor !== mixAtStart || ctx !== _ctx || _stemDecodeBlocked(); // a graph rebuild restarts it
	const phase = (
		name: StemLoadPhase,
		progress: StemFetchProgress | null = null,
		reason: string | null = null
	): void => {
		if (!stale()) st.stems = loadingStemDeckState({ phase: name, progress, reason });
	}; // STEM-48: every stage is a NAMED on-deck state
	let built: AlignedStemDeckProcessor | null = null;
	try {
		const probe = await time(
			'probeStem',
			awaitStemArtifact(stableId, {
				isStale: stale,
				onHydrating: (progress) => phase('fetching', progress)
			})
		);
		if (probe === null || stale()) return;
		if (probe.status !== 'ready') {
			// A settled "this track has no bundle". Not an error, and not a
			// spinner: the deck is finished loading.
			st.stems = unavailableStemDeckState(probe.error);
			stages.total = Math.round(performance.now() - t0);
			recordPerfTiming(`deck-stems-none sid=${stableId.slice(0, 12)}`, stages, deck);
			return;
		}
		const layout = probe.manifest.layout;
		const layoutParts = STEM_LAYOUT_PART_NAMES[layout];
		phase('downloading');
		const encodedParts = await time('fetchStems', fetchStemAudioArrayBuffers(stableId, layout));
		if (stale()) return;
		// Q18: four workers, not four awaits on WebKit's single decode thread.
		// prettier-ignore
		const decoded = await time('decodeStems', decodeStemBuffers(ctx, encodedParts, layoutParts, { deck, stale, onDeferred: () => phase('waiting', null, STEM_HELD_BY_PRESSURE), onStart: () => phase('decoding') }));
		if (stale()) return;
		// STEM-8302: trailing codec slack (an extra MP3 packet, resample rounding) is conformed to the source; a real mismatch still refuses below.
		const outMs = st.edit_out_ms;
		const buffersForConform: StemBuffers = {};
		for (const [part, buffer] of Object.entries(decoded.buffers)) {
			if (buffer === undefined) continue;
			buffersForConform[part as keyof StemBuffers] = truncateAtOutPoint(ctx, buffer, outMs);
		}
		const stemBuffers = conformStemBuffersToFrameCount(
			ctx,
			buffersForConform,
			mixBuffer.length,
			mixBuffer.sampleRate
		);
		const created = await time(
			'stemProcessorCreate',
			AlignedStemDeckProcessor.create(ctx, stemBuffers, {
				onProcessorError: (error: unknown) => {
					if (_rt[deck].processor === built) _recordProcessorFailure(deck, error);
				}
			})
		);
		built = created.processor;
		if (stale()) {
			_retireProcessor(built);
			return;
		}
		if (
			created.alignment.sample_rate_hz !== mixBuffer.sampleRate ||
			created.alignment.frame_count !== mixBuffer.length ||
			Math.abs(created.alignment.duration_ms - mixBuffer.duration * 1000) >
				1000 / mixBuffer.sampleRate
		) {
			_retireProcessor(built);
			throw new Error(
				`stem/source alignment mismatch: source ${mixBuffer.sampleRate}Hz, ` +
					`${mixBuffer.length} frames, ${mixBuffer.duration}s; stems ` +
					`${created.alignment.sample_rate_hz}Hz, ${created.alignment.frame_count} ` +
					`frames, ${created.alignment.duration_ms / 1000}s`
			);
		}
		const readyState = readyStemDeckState(
			{ source: probe.manifest.source, model: probe.manifest.model, layout },
			created.alignment
		);
		applyStemCarry(deck, token, readyState, created.processor); // DECKGAP-7691: "reset stems" off keeps the previous mix
		// AUDFID-02: neutral controls play the original file; the stems wait for the first touch.
		// prettier-ignore
		if (deckPlaysOriginalFile(readyState.controls)) {
			_holdNeutralStems(deck, { token, processor: created.processor, state: readyState });
			built = null;
			stages.total = Math.round(performance.now() - t0);
			recordPerfTiming(`deck-stems sid=${stableId.slice(0, 12)}`, stages, deck, {
				...decoded.labels,
				landed: 'held_neutral',
				pcm_handoff: created.pcmHandoff
			});
			return;
		}
		// STEM-47: a playing deck takes the stems live instead of holding them until it stops.
		// prettier-ignore
		const landed = await time('landStems', _landStems(deck, { token, processor: created.processor, state: { ...readyState, playback: 'stems' } }, ctx, stale));
		built = null; // landed, held as pendingStemUpgrade, or retired as stale: no longer this function's to free
		if (landed === 'deferred') stages.deferredToStop = 1;
		stages.total = Math.round(performance.now() - t0);
		recordPerfTiming(`deck-stems sid=${stableId.slice(0, 12)}`, stages, deck, {
			...decoded.labels,
			landed,
			pcm_handoff: created.pcmHandoff
		});
	} catch (error) {
		if (built !== null) _retireProcessor(built);
		if (stale()) return;
		const message = error instanceof Error ? error.message : String(error);
		st.stems = { ...unavailableStemDeckState(message), status: 'error' };
		stages.failedAt = Math.round(performance.now() - t0);
		recordPerfTiming(`deck-stems-fail sid=${stableId.slice(0, 12)}`, stages, deck);
		pushToast(`Deck ${deck} stems unavailable - ${message}`, 'error');
	}
}

export { _withDeckSwap };

/** Test seam (PERFMODE-15): the lazy stem upgrade exactly as `load` and the graph rebuild run it (node has no context) */
export const upgradeDeckStemsForTest = (
	deck: DeckId,
	stableId: string,
	mixBuffer: AudioBuffer
): Promise<void> =>
	_upgradeDeckStems(deck, stableId, _rt[deck].loadToken, _ctx as AudioContext, mixBuffer);
