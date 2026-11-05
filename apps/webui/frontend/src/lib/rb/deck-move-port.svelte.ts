/**
 * DECKMOVE-01 deck move: the Web Audio engine behind deck-move.ts's port. Every
 * method is the engine's own public call, so a moved deck is in exactly the
 * state the same clicks would have put it in. The Rust (Mac native) engine is
 * refused by the dispatcher before this is built (`deck_move` is WEB_AUDIO_ONLY).
 *
 * -Claude
 */
import { colorFx, applyColorFxSettings } from '$lib/rb/color-fx.svelte';
import {
	engine,
	getMasterMode,
	scheduleDeckHandover,
	startDeckAlignedTo,
	waitForContextSec
} from '$lib/rb/audio-engine.svelte';
import { deckStates, mixerState, pitchRanges } from '$lib/player/state.svelte';
import type { DeckId } from './deck-id';
import type { DeckMovePort } from './deck-move';

// ----- configuration ------------------------------------------------------
/** A same-track stem load is a cache hit; this bounds a cold one. */
const STEMS_READY_TIMEOUT_MS = 30_000;
const STEMS_POLL_MS = 50;

async function _waitStemsReady(deck: DeckId): Promise<void> {
	const stems = () => deckStates[deck].stems;
	if (stems().status !== 'ready' && stems().status !== 'loading') await engine.retryStems(deck);
	const deadline = performance.now() + STEMS_READY_TIMEOUT_MS;
	while (stems().status !== 'ready') {
		if (stems().status === 'error') throw new Error(`deck ${deck} stems failed to load: ${stems().error ?? 'no detail'}`);
		if (performance.now() > deadline) {
			throw new Error(`deck ${deck} stems not ready after ${STEMS_READY_TIMEOUT_MS} ms (status ${stems().status}); the stem settings were not copied`);
		}
		await new Promise((resolve) => setTimeout(resolve, STEMS_POLL_MS));
	}
}

export function webAudioDeckMovePort(): DeckMovePort {
	return {
		deck: (deck) => deckStates[deck],
		channel: (deck) => mixerState.channels[deck],
		colorFx: (deck) => ({ effect: colorFx[deck].effect, echo_beats: colorFx[deck].echo_beats }),
		pitchRange: (deck) => pitchRanges[deck],
		masterLocked: () => getMasterMode() === 'locked',
		load: (deck, stableId) => engine.load(deck, stableId),
		unload: (deck) => engine.unload(deck),
		pause: (deck) => engine.pause(deck),
		setPitchRange: (deck, range) => engine.setPitchRange(deck, range),
		setTempoRatio: (deck, ratio) => engine.setTempoRatio(deck, ratio),
		setMasterTempo: (deck, enabled) => engine.setMasterTempo(deck, enabled),
		setKeyShift: (deck, semitones) => engine.setKeyShift(deck, semitones),
		setKeySync: (deck, enabled, options) => engine.setKeySync(deck, enabled, options),
		setQuantize: (deck, enabled) => engine.setQuantize(deck, enabled),
		setQuantizeGrid: (deck, grid) => engine.setQuantizeGrid(deck, grid),
		setSyncMode: (deck, mode) => engine.setSyncMode(deck, mode),
		setBeatSync: (deck, enabled) => engine.setBeatSync(deck, enabled),
		setSlip: (deck, enabled) => engine.setSlip(deck, enabled),
		setLoop: (deck, loop, quantize) => engine.setLoop(deck, loop, quantize),
		setTrim: (deck, value) => engine.setTrim(deck, value),
		setEq: (deck, band, value) => engine.setEq(deck, band, value),
		setFilter: (deck, value) => engine.setFilter(deck, value),
		applyColorFx: (deck, settings) => void applyColorFxSettings({ deck, ...settings }),
		setStemEqMode: (deck, enabled) => engine.setStemEqMode(deck, enabled),
		waitStemsReady: _waitStemsReady,
		setStemMute: (deck, stem, muted) => engine.setStemMute(deck, stem, muted),
		setStemSolo: (deck, stem, solo) => engine.setStemSolo(deck, stem, solo),
		setStemGain: (deck, stem, value) => engine.setStemGain(deck, stem, value),
		setFader: (deck, value) => engine.setFader(deck, value),
		assignChannel: (deck, assign) => engine.assignChannel(deck, assign),
		seekPaused: (deck, ms) => engine.quantizedSeek(deck, ms, true),
		startAlignedTo: startDeckAlignedTo,
		scheduleHandover: scheduleDeckHandover,
		waitUntilContextSec: (sec) => waitForContextSec(sec),
		setDeckMaster: (deck, options) => engine.setDeckMaster(deck, options)
	};
}
