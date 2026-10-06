/**
 * Runner half of the four-deck performance preset (see performance-preset.ts
 * for the contract): track readiness, the phased plan, start and stop, and the
 * autoplay probe. Split out (round 2 bundle trim) so only /performance/preload1
 * loads it; the root layout and stores need just the route predicates.
 */
import type { PerformanceCommand, PerformanceState } from '$lib/rb/performance-ipc.svelte';
import { frameOrTimeout } from '$lib/rb/frame-backstop';
import { MUTED_MASTER_VOLUME } from '$lib/rb/performance-preset-constants';
import {
	DEFAULT_AUTOPLAY_PROBE_TIMEOUT_MS,
	DEFAULT_PRESENTATION_TIMEOUT_MS,
	DEFAULT_STOP_TIMEOUT_MS,
	LOOP_BOUNDARY_TOLERANCE_MS,
	PRESET_DECK_IDS,
	PerformancePresetPendingError,
	nearestPresetBeatIndex,
	orderedPresetDecks,
	validatePerformancePreset,
	type PerformanceDeckPreset,
	type PerformanceMixerChannelPreset,
	type PerformancePreset,
	type PerformancePresetDriver,
	type PerformancePresetPlan
} from '$lib/rb/performance-preset';

export function assertPresetTracksReady(
	preset: PerformancePreset,
	state: PerformanceState
): void {
	for (const definition of orderedPresetDecks(preset)) {
		const deck = state.decks[definition.deck];
		const prefix = `deck ${definition.deck} is not preset-ready`;
		if (deck.stable_id !== definition.stable_id) {
			throw new Error(`${prefix}: expected ${definition.stable_id}, got ${String(deck.stable_id)}`);
		} else if (deck.duration_ms === null || !Number.isFinite(deck.duration_ms) || deck.duration_ms <= 0) {
			throw new Error(`${prefix}: decoded duration is unavailable`);
		} else if (deck.beatgrid_ms.length === 0) {
			throw new Error(`${prefix}: real PQTZ beat grid is empty`);
		} else if (deck.processor_error !== null) {
			throw new Error(`${prefix}: processor failed: ${deck.processor_error}`);
		} else if (deck.command_error !== null) {
			throw new Error(`${prefix}: command failed: ${deck.command_error}`);
		} else if (deck.playing || deck.audible || deck.transport_pending) {
			throw new Error(`${prefix}: transport must be fully stopped`);
		}
		if (definition.position_ms > deck.duration_ms) {
			throw new Error(
				`${prefix}: position_ms ${definition.position_ms} exceeds decoded duration ${deck.duration_ms}`
			);
		}
		if (definition.loop.in_ms > deck.duration_ms) {
			throw new Error(
				`${prefix}: loop.in_ms ${definition.loop.in_ms} exceeds decoded duration ${deck.duration_ms}`
			);
		}
		let startIndex: number;
		try {
			startIndex = nearestPresetBeatIndex(deck.beatgrid_ms, definition.loop.in_ms);
		} catch (error) {
			throw new Error(`${prefix}: ${error instanceof Error ? error.message : String(error)}`, {
				cause: error
			});
		}
		const remainingBeats = deck.beatgrid_ms.length - startIndex - 1;
		if (definition.loop.beats > remainingBeats) {
			throw new Error(
				`${prefix}: loop requires ${definition.loop.beats} beats from PQTZ index ` +
					`${startIndex}, but only ${remainingBeats} remain`
			);
		}
		const loopOutMs = deck.beatgrid_ms[startIndex + definition.loop.beats];
		const loopInMs = deck.beatgrid_ms[startIndex];
		if (Math.abs(loopInMs - definition.loop.in_ms) > LOOP_BOUNDARY_TOLERANCE_MS) {
			throw new Error(
				`${prefix}: PQTZ resolves loop in ${loopInMs}, not captured ${definition.loop.in_ms}`
			);
		}
		if (Math.abs(loopOutMs - definition.loop.out_ms) > LOOP_BOUNDARY_TOLERANCE_MS) {
			throw new Error(
				`${prefix}: ${definition.loop.beats} PQTZ beats resolve loop out ${loopOutMs}, ` +
					`not captured ${definition.loop.out_ms}`
			);
		}
		if (loopOutMs > deck.duration_ms) {
			throw new Error(
				`${prefix}: loop out ${loopOutMs} exceeds decoded duration ${deck.duration_ms}`
			);
		}
	}
}

// ------------------------------------------------------------- application

function _configureDeckCommands(definition: PerformanceDeckPreset): PerformanceCommand[] {
	const deck = definition.deck;
	// Q1-DEFAULT-ON: a preset is the operator's own saved configuration and
	// owns every control while it runs, so its Quantize writes carry user
	// provenance (the transient off lands the cue exactly; the last write is
	// the preset's own choice, held until the deck's next load).
	return [
		{ type: 'pitch_range', deck, range: definition.pitch_range },
		{ type: 'quantize', deck, enabled: false, by_user: true },
		{ type: 'sync_mode', deck, mode: definition.sync_mode },
		{ type: 'beat_sync', deck, enabled: definition.beat_sync_enabled },
		{ type: 'master_tempo', deck, enabled: definition.master_tempo_enabled },
		{ type: 'tempo', deck, ratio: definition.tempo_ratio },
		{ type: 'seek', deck, position_ms: definition.position_ms },
		{
			type: 'beat_loop',
			deck,
			beats: definition.loop.beats,
			start_ms: definition.loop.in_ms
		},
		{ type: 'quantize', deck, enabled: definition.quantize_enabled, by_user: true }
	];
}

function _mixerChannelCommands(
	deck: PerformanceDeckPreset['deck'],
	channel: PerformanceMixerChannelPreset
): PerformanceCommand[] {
	return [
		{ type: 'trim', deck, value: channel.trim },
		{ type: 'eq', deck, band: 'high', value: channel.high },
		{ type: 'eq', deck, band: 'mid', value: channel.mid },
		{ type: 'eq', deck, band: 'low', value: channel.low },
		{ type: 'filter', deck, value: channel.filter },
		{ type: 'fader', deck, value: channel.fader },
		{ type: 'assign', deck, assign: channel.assign }
	];
}

export function buildPerformancePresetPlan(preset: PerformancePreset): PerformancePresetPlan {
	validatePerformancePreset(preset);
	const decks = orderedPresetDecks(preset);
	const playbackDecks = [
		decks.find((deck) => deck.deck === preset.master_deck),
		...decks.filter((deck) => deck.deck !== preset.master_deck)
	];
	if (playbackDecks.some((deck) => deck === undefined)) {
		throw new Error(`validated preset has no master deck definition`);
	}
	const master = playbackDecks[0]!;
	return {
		load: [
			{ type: 'master_volume', value: MUTED_MASTER_VOLUME },
			...decks.map((definition) => ({
				type: 'load' as const,
				deck: definition.deck,
				stable_id: definition.stable_id
			}))
		],
		configure: [
			...decks.flatMap(_configureDeckCommands),
			...PRESET_DECK_IDS.flatMap((deck) =>
				_mixerChannelCommands(deck, preset.mixer.channels[deck])
			),
			{ type: 'crossfader', value: preset.mixer.crossfader },
			{ type: 'master', deck: preset.master_deck }
		],
		start: playbackDecks.map((definition) => ({
				type: 'play' as const,
				deck: definition!.deck,
				playing: true
			})),
		synchronize: { type: 'tempo', deck: master.deck, ratio: master.tempo_ratio },
		unmute: { type: 'master_volume', value: preset.mixer.master }
	};
}

function _sameNumber(actual: number, expected: number): boolean {
	return Math.abs(actual - expected) <= LOOP_BOUNDARY_TOLERANCE_MS;
}

function _assertPresetOutputMuted(state: PerformanceState): void {
	if (!_sameNumber(state.mixer.master, MUTED_MASTER_VOLUME)) {
		throw new Error(`performance preset output must stay hard-muted before shared presentation`);
	}
}

function _assertPresetDeckStaticState(
	preset: PerformancePreset,
	state: PerformanceState,
	requirePausedCursor: boolean
): void {
	for (const definition of orderedPresetDecks(preset)) {
		const deck = state.decks[definition.deck];
		if (deck.stable_id !== definition.stable_id) {
			throw new Error(`deck ${definition.deck} configured stable_id does not match preset`);
		} else if (!_sameNumber(deck.pitch, definition.tempo_ratio)) {
			throw new Error(`deck ${definition.deck} configured tempo ratio does not match preset`);
		} else if (deck.pitch_range !== definition.pitch_range) {
			throw new Error(`deck ${definition.deck} configured pitch range does not match preset`);
		} else if (deck.quantize_enabled !== definition.quantize_enabled) {
			throw new Error(`deck ${definition.deck} configured Quantize does not match preset`);
		} else if (deck.beat_sync_enabled !== definition.beat_sync_enabled) {
			throw new Error(`deck ${definition.deck} configured Beat Sync does not match preset`);
		} else if (deck.master_tempo_enabled !== definition.master_tempo_enabled) {
			throw new Error(`deck ${definition.deck} configured Master Tempo does not match preset`);
		} else if (deck.sync_mode !== definition.sync_mode) {
			throw new Error(`deck ${definition.deck} configured sync mode does not match preset`);
		} else if (
			deck.loop === null ||
			!deck.loop.engaged ||
			!_sameNumber(deck.loop.in_ms, definition.loop.in_ms) ||
			!_sameNumber(deck.loop.out_ms, definition.loop.out_ms) ||
			deck.loop.beat_length !== definition.loop.beats
		) {
			throw new Error(`deck ${definition.deck} configured loop does not match preset`);
		} else if (deck.is_master !== (definition.deck === preset.master_deck)) {
			throw new Error(`deck ${definition.deck} configured master selection does not match preset`);
		} else if (deck.sync_error !== null) {
			throw new Error(`deck ${definition.deck} sync failed: ${deck.sync_error}`);
		} else if (deck.processor_error !== null) {
			throw new Error(`deck ${definition.deck} processor failed: ${deck.processor_error}`);
		} else if (deck.command_error !== null) {
			throw new Error(`deck ${definition.deck} command failed: ${deck.command_error}`);
		}
		if (requirePausedCursor && !_sameNumber(deck.position_ms, definition.position_ms)) {
			throw new Error(`deck ${definition.deck} configured cursor does not match preset`);
		}
	}
}

function _assertPresetMixer(
	preset: PerformancePreset,
	state: PerformanceState,
	expectedMasterVolume: number = preset.mixer.master
): void {
	if (!_sameNumber(state.mixer.master, expectedMasterVolume)) {
		throw new Error(`configured master volume does not match preset`);
	} else if (!_sameNumber(state.mixer.crossfader, preset.mixer.crossfader)) {
		throw new Error(`configured crossfader does not match preset`);
	}
	for (const deck of PRESET_DECK_IDS) {
		const actual = state.mixer.channels[deck];
		const expected = preset.mixer.channels[deck];
		if (
			!_sameNumber(actual.trim, expected.trim) ||
			!_sameNumber(actual.eq_high, expected.high) ||
			!_sameNumber(actual.eq_mid, expected.mid) ||
			!_sameNumber(actual.eq_low, expected.low) ||
			!_sameNumber(actual.filter, expected.filter) ||
			!_sameNumber(actual.fader, expected.fader) ||
			actual.assign !== expected.assign
		) {
			throw new Error(`configured mixer channel ${deck} does not match preset`);
		}
	}
}

export function assertPerformancePresetConfigured(
	preset: PerformancePreset,
	state: PerformanceState
): void {
	_assertPresetDeckStaticState(preset, state, true);
	_assertPresetMixer(preset, state, MUTED_MASTER_VOLUME);
	if (state.master_deck !== preset.master_deck) {
		throw new Error(`configured master deck does not match preset`);
	} else if (state.last_error !== null) {
		throw new Error(`configured preset contains command error: ${state.last_error}`);
	}
}

export function assertPerformancePresetPresented(
	preset: PerformancePreset,
	state: PerformanceState
): void {
	_assertPresetDeckStaticState(preset, state, false);
	_assertPresetMixer(preset, state);
	if (state.master_deck !== preset.master_deck) {
		throw new Error(`presented master deck does not match preset`);
	}
	for (const definition of orderedPresetDecks(preset)) {
		const deck = state.decks[definition.deck];
		if (
			!deck.playing ||
			!deck.audible ||
			deck.transport_pending ||
			deck.transport_clock.presented_revision !== deck.transport_clock.desired_revision
		) {
			throw new PerformancePresetPendingError(
				`deck ${definition.deck} transport has not reached the audio output`
			);
		} else if (
			deck.position_ms < definition.loop.in_ms ||
			deck.position_ms >= definition.loop.out_ms
		) {
			throw new PerformancePresetPendingError(
				`deck ${definition.deck} presented position is outside its preset loop`
			);
		}
	}
}



export async function waitForPerformancePresetPresented(
	preset: PerformancePreset,
	query: () => PerformanceState,
	assertCurrent: () => void,
	timeoutMs: number = DEFAULT_PRESENTATION_TIMEOUT_MS
): Promise<PerformanceState> {
	if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
		throw new RangeError(`performance preset presentation timeout must be positive`);
	}
	const deadline = performance.now() + timeoutMs;
	let lastPending: PerformancePresetPendingError | null = null;
	while (performance.now() < deadline) {
		assertCurrent();
		const state = query();
		try {
			assertPerformancePresetPresented(preset, state);
			return state;
		} catch (error) {
			if (!(error instanceof PerformancePresetPendingError)) throw error;
			lastPending = error;
		}
		await frameOrTimeout();
		assertCurrent();
	}
	throw new Error(
		`performance preset ${preset.id} did not reach audio output within ${timeoutMs}ms: ` +
			`${lastPending?.message ?? 'no presentation sample was observed'}`,
		{ cause: lastPending ?? undefined }
	);
}

export function assertPerformancePresetStopped(
	preset: PerformancePreset,
	state: PerformanceState
): void {
	for (const definition of orderedPresetDecks(preset)) {
		const deck = state.decks[definition.deck];
		if (
			deck.playing ||
			deck.audible ||
			deck.transport_pending ||
			deck.transport_clock.presented_revision !== deck.transport_clock.desired_revision
		) {
			throw new PerformancePresetPendingError(
				`deck ${definition.deck} has not reached a fully presented stop`
			);
		}
	}
	if (state.master_deck !== null) {
		throw new PerformancePresetPendingError(
			`stopped performance preset still has master deck ${state.master_deck}`
		);
	}
}

export async function waitForPerformancePresetStopped(
	preset: PerformancePreset,
	query: () => PerformanceState,
	timeoutMs: number = DEFAULT_STOP_TIMEOUT_MS
): Promise<PerformanceState> {
	if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
		throw new RangeError(`performance preset stop timeout must be positive`);
	}
	const deadline = performance.now() + timeoutMs;
	let lastPending: PerformancePresetPendingError | null = null;
	while (performance.now() < deadline) {
		const state = query();
		try {
			assertPerformancePresetStopped(preset, state);
			return state;
		} catch (error) {
			if (!(error instanceof PerformancePresetPendingError)) throw error;
			lastPending = error;
		}
		await frameOrTimeout();
	}
	throw new Error(
		`performance preset ${preset.id} did not stop within ${timeoutMs}ms: ` +
			`${lastPending?.message ?? 'no stopped transport sample was observed'}`,
		{ cause: lastPending ?? undefined }
	);
}

export async function probeWebAudioAutoplay(
	timeoutMs: number = DEFAULT_AUTOPLAY_PROBE_TIMEOUT_MS
): Promise<boolean> {
	if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
		throw new RangeError(`Web Audio autoplay probe timeout must be positive`);
	}
	if (typeof window === 'undefined' || window.AudioContext === undefined) {
		throw new Error(`Web Audio autoplay probe requires window.AudioContext`);
	}
	const context = new window.AudioContext();
	if (context.state === 'running') {
		await context.close();
		return true;
	}
	let timeoutId: ReturnType<typeof setTimeout> | null = null;
	const timedOut = new Promise<{ kind: 'timeout' }>((resolve) => {
		timeoutId = setTimeout(() => resolve({ kind: 'timeout' }), timeoutMs);
	});
	const resumed = context.resume().then(
		() => ({ kind: 'resumed' as const, allowed: context.state === 'running' }),
		(error: unknown) => ({ kind: 'rejected' as const, error })
	);
	const outcome = await Promise.race([resumed, timedOut]);
	if (timeoutId !== null) clearTimeout(timeoutId);
	if (outcome.kind === 'timeout') {
		void resumed.then((settled) => {
			if (settled.kind === 'rejected' && context.state !== 'closed') {
				console.error('Web Audio autoplay probe resume failed after timeout', settled.error);
			}
		});
		void context
			.close()
			.catch((error: unknown) => console.error('Web Audio autoplay probe cleanup failed', error));
		return false;
	}
	await context.close();
	if (outcome.kind === 'rejected') throw outcome.error;
	return outcome.allowed;
}

export async function preparePerformancePreset(
	preset: PerformancePreset,
	driver: PerformancePresetDriver,
	assertCurrent: () => void
): Promise<void> {
	const plan = buildPerformancePresetPlan(preset);

	driver.setPhase('loading');
	for (const command of plan.load) {
		assertCurrent();
		await driver.dispatch(command);
		assertCurrent();
	}
	assertPresetTracksReady(preset, driver.query());

	driver.setPhase('configuring');
	for (const command of plan.configure) {
		assertCurrent();
		await driver.dispatch(command);
		assertCurrent();
	}
	assertPerformancePresetConfigured(preset, driver.query());
}

export async function startPerformancePreset(
	preset: PerformancePreset,
	driver: PerformancePresetDriver,
	assertCurrent: () => void
): Promise<PerformanceState> {
	const plan = buildPerformancePresetPlan(preset);
	driver.setPhase('starting');
	let finalState: PerformanceState | null = null;
	try {
		_assertPresetOutputMuted(driver.query());
		for (const command of plan.start) {
			if (command.type !== 'play' || !command.playing) {
				throw new Error(`performance preset start plan contains a non-start command`);
			}
			_assertPresetOutputMuted(driver.query());
			assertCurrent();
			finalState = await driver.dispatch(command);
			assertCurrent();
			_assertPresetOutputMuted(driver.query());
		}
		if (finalState === null) throw new Error(`performance preset emitted no playback commands`);
		assertCurrent();
		await driver.dispatch(plan.synchronize);
		assertCurrent();
		_assertPresetOutputMuted(driver.query());
		const mutedPreset: PerformancePreset = {
			...preset,
			mixer: { ...preset.mixer, master: MUTED_MASTER_VOLUME }
		};
		await waitForPerformancePresetPresented(mutedPreset, driver.query, assertCurrent);
		assertCurrent();
		finalState = await driver.dispatch(plan.unmute);
		assertCurrent();
		return await waitForPerformancePresetPresented(preset, driver.query, assertCurrent);
	} catch (startError) {
		const rollbackErrors: unknown[] = [];
		try {
			if (!_sameNumber(driver.query().mixer.master, MUTED_MASTER_VOLUME)) {
				await driver.dispatch({ type: 'master_volume', value: MUTED_MASTER_VOLUME });
			}
			_assertPresetOutputMuted(driver.query());
		} catch (muteError) {
			rollbackErrors.push(muteError);
		}
		try {
			await _stopPerformancePresetDecks(preset, driver);
		} catch (stopError) {
			rollbackErrors.push(stopError);
		}
		if (rollbackErrors.length > 0) {
			throw new AggregateError(
				[startError, ...rollbackErrors],
				`performance preset start failed and its mute/full-stop rollback failed`,
				{ cause: startError }
			);
		}
		throw startError;
	}
}

async function _stopPerformancePresetDecks(
	preset: PerformancePreset,
	driver: PerformancePresetDriver
): Promise<PerformanceState> {
	const stopErrors: unknown[] = [];
	for (const definition of orderedPresetDecks(preset).reverse()) {
		try {
			await driver.dispatch({ type: 'play', deck: definition.deck, playing: false });
		} catch (error) {
			stopErrors.push(error);
		}
	}
	try {
		const stopped = await waitForPerformancePresetStopped(preset, driver.query);
		if (stopErrors.length === 0) return stopped;
	} catch (error) {
		stopErrors.push(error);
	}
	throw new AggregateError(
		stopErrors,
		`performance preset ${preset.id} failed to reach a fully presented stop`
	);
}

export async function stopPerformancePreset(
	preset: PerformancePreset,
	driver: PerformancePresetDriver
): Promise<PerformanceState> {
	driver.setPhase('stopping');
	return _stopPerformancePresetDecks(preset, driver);
}
