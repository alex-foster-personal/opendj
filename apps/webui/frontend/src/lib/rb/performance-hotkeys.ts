// Shared keyboard shortcuts for /performance. Keep KISS: one module, window
// keydown, ignore when typing in inputs. Loop targets track last interaction
// (engage/resize/hover >250ms on a deck's loop cluster). Space toggles play
// on the most recent channel (load / play / loop target), except while a
// keyboard-interactive control owns focus. Tab always moves browser focus.
// C cues, V toggles beat sync and K master tempo on that same recent deck;
// Backquote (Shift reverses) moves it to the next loaded deck (KEYB-11383).
// Cmd+, / Ctrl+, is owned by settings/hotkeys.ts (meta/ctrl ignored here).
// Digits 1-8 are the transport deck's hot cues (#10354); Shift saves an empty
// slot, Cmd+Shift clears.
// #10357: , . (Shift coarse) step tempo; Shift+K reset; P cycles pitch range.
import { DECK_IDS, getDeckState, pitchRanges } from '$lib/rb/audio-engine.svelte';
import { runPerformanceCommandFromUi, uiPrefs } from '$lib/rb/performance-ipc.svelte';
import {
	cycleRecentDeck,
	getRecentDeck,
	noteRecentDeck,
	resolveKeyboardDeck,
	type DeckPresence
} from '$lib/rb/recent-deck';
import {
	mostRecentPendingLoadPlay,
	setPendingLoadPlayIntent,
	type DeckId
} from '$lib/rb/deck-slots';
import { createDeckHotCueActions } from '$lib/rb/deck-hot-cue-actions';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import { selectedKnobIds } from '$lib/rb/knob-control.svelte';
import { isSettingsOpen } from '$lib/settings/overlay.svelte';
import { deckHoverUi } from '$lib/rb/deck-hover.svelte';
import { armPinPlacement } from './feedback-store.svelte';
import {
	buildRecentDeckCommand,
	handlePerformanceShortcutKeydown,
	type PerformanceShortcutHandlers,
	type RecentDeckVerb
} from './performance-shortcut-routing';
import { gridJumpEnabled } from './grid-jump-flag';
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import {
	edgeSamplerUi,
	fireEdgeTab,
	flipEdgePage,
	type EdgeId
} from '$lib/player/sampler/edge-sampler-ui.svelte';
import { nativeShellKind } from '$lib/shell/native-shell';
import { tempoKeyCommand, type TempoKeyAction } from './tempo-keys';
import {
	activeKeyboardPreset,
	handleKeymapKeydown,
	legacyPerformanceChordsActive
} from '$lib/keyboard/keymap-runtime.svelte';

// Re-exported so noteLoopInteraction's callers (e.g. LoopSafetyControls.svelte)
// can take DeckId from here instead of a fresh direct import of deck-slots.ts,
// which sits at its frontend.max_fan_in allowance (same pairing as DECK_IDS
// alongside DeckId in $lib/player/constants).
export type { DeckId };

const HOVER_ARM_MS = 250;
const MIN_BEATS = 1;
const MAX_BEATS = 512;

let lastLoopDeck: DeckId | null = null;
let hoverTimer: ReturnType<typeof setTimeout> | null = null;
let hoverDeck: DeckId | null = null;

export function noteLoopInteraction(deck: DeckId): void {
	lastLoopDeck = deck;
	noteRecentDeck(deck);
}

export function armLoopHover(deck: DeckId): void {
	hoverDeck = deck;
	if (hoverTimer !== null) clearTimeout(hoverTimer);
	hoverTimer = setTimeout(() => {
		if (hoverDeck === deck) {
			lastLoopDeck = deck;
			noteRecentDeck(deck);
		}
		hoverTimer = null;
	}, HOVER_ARM_MS);
}

export function clearLoopHover(deck: DeckId): void {
	if (hoverDeck === deck) hoverDeck = null;
	if (hoverTimer !== null) {
		clearTimeout(hoverTimer);
		hoverTimer = null;
	}
}

function _deckPresence(): DeckPresence[] {
	return DECK_IDS.map((id) => ({
		id,
		loaded: getDeckState(id).stable_id !== null,
		playing: getDeckState(id).playing
	}));
}

function _resolveTransportDeck(): DeckId | null {
	const deck = resolveKeyboardDeck(getRecentDeck(), _deckPresence());
	if (deck !== null && deck !== getRecentDeck()) noteRecentDeck(deck);
	return deck;
}

/**
 * Q1: `pressT0Ms` is the keydown's own `event.timeStamp`, on the
 * `performance.now()` epoch, threaded from the listener instead of re-read
 * here.
 *
 * Space is the most latency-sensitive press in the app - it is how a DJ starts
 * a track without looking at the screen - and it is also the path with the most
 * to hide before the stamp arrives: the browser queues the key, dispatches to
 * this listener, and only then does anything measurable begin. A
 * `performance.now()` taken downstream starts the clock after all of that and
 * reports a number smaller than the wait the operator actually had.
 *
 * An EMPTY deck returns before dispatching anything, which is what keeps the
 * instrument honest: no command means no schedule, so no press row, so silence
 * can never be quoted as a latency.
 */
async function _toggleRecentPlay(pressT0Ms?: number, quantize?: boolean): Promise<void> {
	const pending = mostRecentPendingLoadPlay();
	if (pending !== null) {
		const desiredPlay = !pending.desiredPlay;
		await runPerformanceCommandFromUi(
			{
				type: 'load_play_intent',
				deck: pending.deck,
				generation: pending.generation,
				desired_play: desiredPlay
			},
			pressT0Ms
		);
		if (quantize === true && desiredPlay) {
			setPendingLoadPlayIntent(pending.deck, pending.generation, desiredPlay, pressT0Ms, true);
		}
		return;
	}
	const deck = _resolveTransportDeck();
	if (deck === null) return;
	const st = getDeckState(deck);
	if (st.stable_id === null) return;
	noteRecentDeck(deck);
	const playing = !st.playing;
	await runPerformanceCommandFromUi(
		{
			type: 'play',
			deck,
			playing,
			...(quantize === true && playing ? { quantize: true } : {})
		},
		pressT0Ms
	);
}

async function _resizeLast(factor: 0.5 | 2): Promise<void> {
	const deck = lastLoopDeck;
	if (deck === null) return;
	const st = getDeckState(deck);
	if (st.loop === null || !st.loop.engaged || st.loop.beat_length === null) return;
	const next =
		factor === 0.5
			? Math.max(MIN_BEATS, Math.floor(st.loop.beat_length / 2))
			: Math.min(MAX_BEATS, st.loop.beat_length * 2);
	if (next === st.loop.beat_length) return;
	await runPerformanceCommandFromUi({
		type: 'beat_loop',
		deck,
		beats: next,
		start_ms: st.loop.in_ms
	});
}

async function _exitLast(): Promise<void> {
	const deck = lastLoopDeck;
	if (deck === null) return;
	const st = getDeckState(deck);
	if (st.loop === null) return;
	await runPerformanceCommandFromUi({ type: 'loop', deck, loop: null });
}

/** DECKUX-6794: jump the transport deck (the last one clicked, else the
 * playing one) on the fixed bar grid. An empty deck sends nothing. */
async function _gridJump(bars: number): Promise<void> {
	const deck = _resolveTransportDeck();
	if (deck === null) return;
	await runPerformanceCommandFromUi({ type: 'grid_jump', deck, bars });
}

/** KEYB-11383: C, V and K act on the transport deck; an empty deck sends nothing. */
async function _recentDeckCommand(verb: RecentDeckVerb): Promise<void> {
	const deck = _resolveTransportDeck();
	const command = buildRecentDeckCommand(verb, deck, deck === null ? null : getDeckState(deck));
	if (command !== null) await runPerformanceCommandFromUi(command);
}

/** KEYB-11383: Backquote moves the recent deck (UI focus state, no bus command). */
function _cycleRecentDeck(direction: 1 | -1): void {
	const presence = _deckPresence();
	const loaded = presence.filter((d) => d.loaded).map((d) => d.id);
	const next = cycleRecentDeck(resolveKeyboardDeck(getRecentDeck(), presence), direction, loaded);
	if (next !== null) noteRecentDeck(next);
}

/** #10357: tempo keys act on the recent deck only; an empty or unset recent deck sends nothing. */
async function _tempoKey(action: TempoKeyAction): Promise<void> {
	const deck = getRecentDeck();
	const st = deck === null ? null : getDeckState(deck);
	const command = tempoKeyCommand(
		action,
		deck === null || st === null
			? null
			: { deck, loaded: st.stable_id !== null, pitch: st.pitch, range: pitchRanges[deck] }
	);
	if (command !== null) await runPerformanceCommandFromUi(command);
}

function _flashHotCuePad(deck: DeckId, slot: HotCueSlot): void {
	document
		.querySelector<HTMLElement>(`[data-testid="hot-cue-${deck}-${slot}"]`)
		?.animate([{ filter: 'brightness(1.8)' }, { filter: 'none' }], { duration: 250 });
}

/** #10354: digit N on the transport deck's hot cue pad N. An empty deck sends nothing. */
async function _hotCueKey(kind: 'trigger' | 'save' | 'clear', slot: HotCueSlot): Promise<void> {
	const deck = _resolveTransportDeck();
	if (deck === null) return;
	const st = getDeckState(deck);
	const filled = st.hot_cues.some((c) => c.slot === slot);
	if (kind === 'trigger') {
		if (filled) await runPerformanceCommandFromUi({ type: 'hot_cue_trigger', deck, slot });
		return;
	}
	if (kind === 'save' && filled) {
		_flashHotCuePad(deck, slot);
		return;
	}
	if (kind === 'clear' && !filled) return;
	const actions = createDeckHotCueActions(
		() => deck,
		() => getDeckState(deck)
	);
	await (kind === 'save' ? actions.saveHotCueAt(slot) : actions.clearHotCueAt(slot));
}

/** True when a pointer press landed on a deck or mixer channel rather than
 * the library, so Cmd+A means "jump back" instead of Select all. */
function _isDeckPress(target: EventTarget | null): boolean {
	if (!(target instanceof Element)) return false;
	return (
		target.closest('[data-deck-hover]') !== null || target.closest('[data-mixer-channel]') !== null
	);
}

export function installPerformanceHotkeys(): () => void {
	let deckFocus = false;
	const onPointer = (e: PointerEvent): void => {
		deckFocus = _isDeckPress(e.target);
	};
	function tryEdgeSamplerHotkey(e: KeyboardEvent): boolean {
		if (!uiPrefs.sampler_enabled || nativeShellKind() !== 'electron') return false;
		if (isTextEntryTarget(e.target) || e.metaKey || e.ctrlKey || e.altKey) return false;
		const edge = edgeSamplerUi.lastEdge as EdgeId;
		if (e.key === ']' || e.key === 'PageDown') {
			flipEdgePage(edge, 1);
			e.preventDefault();
			return true;
		}
		if (e.key === '[' || e.key === 'PageUp') {
			flipEdgePage(edge, -1);
			e.preventDefault();
			return true;
		}
		const digit = '1234567890'.indexOf(e.key);
		if (digit >= 0) {
			fireEdgeTab(edge, digit);
			e.preventDefault();
			return true;
		}
		return false;
	}

	const onKey = (e: KeyboardEvent): void => {
		if (tryEdgeSamplerHotkey(e)) return;
		const handlers: PerformanceShortcutHandlers = {
			toggleRecentPlay: (pressT0Ms, quantize) => {
				void _toggleRecentPlay(pressT0Ms, quantize || undefined);
			},
			resizeLast: (factor) => {
				void _resizeLast(factor);
			},
			exitLast: () => {
				void _exitLast();
			},
			armPinPlacement,
			// Linked zoom moves every deck; unlinked, the deck under the pointer,
			// so with no deck hovered the key does nothing rather than move them all.
			waveZoomStep: (direction) => {
				const deck = uiPrefs.wave_zoom_linked ? null : deckHoverUi.deckId;
				if (deck === null && !uiPrefs.wave_zoom_linked) return;
				void runPerformanceCommandFromUi(
					deck === null
						? { type: 'wave_zoom_step', direction }
						: { type: 'wave_zoom_step', direction, deck }
				);
			},
			gridJump: (bars) => {
				void _gridJump(bars);
			},
			recentDeckCommand: (verb) => {
				void _recentDeckCommand(verb);
			},
			cycleRecentDeck: _cycleRecentDeck,
			tempoKey: (action) => {
				void _tempoKey(action);
			},
			hotCueTrigger: (slot) => {
				void _hotCueKey('trigger', slot).catch(() => {});
			},
			hotCueSave: (slot) => {
				void _hotCueKey('save', slot).catch(() => {});
			},
			hotCueClear: (slot) => {
				void _hotCueKey('clear', slot).catch(() => {});
			}
		};
		if (handleKeymapKeydown(e, handlers)) return;
		if (!legacyPerformanceChordsActive(activeKeyboardPreset())) return;
		handlePerformanceShortcutKeydown(e, handlers, {
			settingsOpen: isSettingsOpen(),
			gridJump: gridJumpEnabled(),
			deckFocus,
			libraryFocus: e.target instanceof Element && e.target.closest('[data-library-root]') !== null,
			dialSelected: selectedKnobIds().length > 0
		});
	};
	window.addEventListener('pointerdown', onPointer, { capture: true });
	window.addEventListener('keydown', onKey, { capture: true });
	return () => {
		window.removeEventListener('pointerdown', onPointer, { capture: true });
		window.removeEventListener('keydown', onKey, { capture: true });
	};
}

/** Fallback: if no interaction yet, prefer any engaged loop (lowest deck id). */
export function ensureLastLoopDeck(): DeckId | null {
	if (lastLoopDeck !== null && getDeckState(lastLoopDeck).loop !== null) return lastLoopDeck;
	for (const d of DECK_IDS) {
		if (getDeckState(d).loop !== null) {
			lastLoopDeck = d;
			return d;
		}
	}
	return lastLoopDeck;
}
