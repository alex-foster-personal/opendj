// Shared keyboard shortcuts for /performance. Keep KISS: one module, window
// keydown, ignore when typing in inputs. Loop targets track last interaction
// (engage/resize/hover >250ms on a deck's loop cluster). Space toggles play
// on the most recent channel (load / play / loop target), except while a
// keyboard-interactive control owns focus. Tab always moves browser focus.
// C cues, V toggles beat sync and K master tempo on that same recent deck;
// Backquote (Shift reverses) moves it to the next loaded deck (KEYB-11383).
// Cmd+, / Ctrl+, is owned by settings/hotkeys.ts (meta/ctrl ignored here).
// Digits 1-8 are the transport deck's hot cues (#10354); Shift saves an empty
// slot, Cmd+Shift clears. The edge sampler takes digits and PageUp / PageDown
// only while one of its edges is awake (#13720).
// #10357: , . (Shift coarse) step tempo; Shift+K reset; P cycles pitch range.
// #9395: W A S D move the keyboard selector, E drills in, Q out, Esc clears it.
// #13708: on a selected leaf, Enter presses its control and the arrows step its dial.
// #14324: the arrows also step a crossfader or pitch slider leaf and nudge a jog leaf's tempo.
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
import {
	KNOB_CFG,
	nudgeKnobOrSelection,
	readKnobValue,
	selectedKnobIds
} from '$lib/rb/knob-control.svelte';
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
import type { SectionJumpDirection } from './section-jump';
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { isModalDialogTarget } from '$lib/keyboard/modal-dialog-target';
import { trackTypeAhead } from '$lib/components/rb/browser/track-table-type-ahead';
import { fireEdgeTab, flipSet, sampler } from '$lib/player/sampler/sampler-store.svelte';
import { awakeSamplerEdge, samplerKeyAction } from '$lib/player/sampler/sampler-keys';
import { nativeShellKind } from '$lib/shell/native-shell';
import { tempoKeyCommand, type TempoKeyAction } from './tempo-keys';
import {
	activeKeyboardPreset,
	handleKeymapKeydown,
	legacyPerformanceChordsActive
} from '$lib/keyboard/keymap-runtime.svelte';
import {
	selId,
	onSelectorRegistryChange,
	selectorRegistryVersion,
	snapshotSelectorNodes
} from '$lib/keyboard/selector-register';
import {
	clearSelectorRingTarget,
	setSelectorRingTarget
} from '$lib/keyboard/selector-ring-state.svelte';
import { createSelectorSession, type SelectorSession } from '$lib/keyboard/selector-session';
import { installSelectorFocusRestore } from '$lib/keyboard/selector-focus-restore';
import {
	setKeyboardSelectorProvider,
	setKeyboardShortcutHandlersProvider
} from '$lib/keyboard/keyboard-agent-handle';
import { runCommand, type SelectorLeafHost } from '$lib/keyboard/command-registry';
import {
	createSelectorLeafHost,
	selectorLeafAdjustTarget,
	type SelectorLeafHostDeps
} from '$lib/keyboard/selector-activate';
import { handleMidiAction } from '$lib/rb/midi/action-glue.svelte';

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
		const result = await runPerformanceCommandFromUi(
			{
				type: 'load_play_intent',
				deck: pending.deck,
				generation: pending.generation,
				desired_play: desiredPlay
			},
			pressT0Ms
		);
		// #12289: a press that never ran (no session, or refused) arms nothing.
		if (result.ok && quantize === true && desiredPlay) {
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

/** Issue #10263: jump the transport deck to its next or previous analysed
 * section start. An empty deck sends nothing. */
async function _sectionJump(direction: SectionJumpDirection): Promise<void> {
	const deck = _resolveTransportDeck();
	if (deck === null) return;
	await runPerformanceCommandFromUi({ type: 'section_jump', deck, direction });
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

/** The selector node the key came from, so the first selector key starts where focus is. */
function _selectorNodeAt(target: EventTarget | null): string | null {
	if (!(target instanceof Element)) return null;
	return target.closest('[data-sel-node]')?.getAttribute('data-sel-node') ?? null;
}

/** #13708: the page and knob registry behind the selected leaf's Enter and arrows. */
const SELECTOR_LEAF_DEPS: SelectorLeafHostDeps = {
	findLeaf: (leafId) => document.querySelector(`[data-sel-node="${CSS.escape(leafId)}"]`),
	readControl: readKnobValue,
	nudgeControl: nudgeKnobOrSelection,
	// #14324: a jog leaf's arrows send the keymap's own tempo nudge action.
	nudgeTempo: (deck, direction) => {
		void handleMidiAction(
			{ type: 'deck_tempo_nudge', deck, direction },
			{ kind: 'button', pressed: true, velocity: 127 }
		);
	},
	keyStep: KNOB_CFG.keyStep
};

/** #14324: the leaf has a knob, jog or slider for the arrows to adjust. */
function _leafAdjustable(leafId: string): boolean {
	return selectorLeafAdjustTarget(leafId, SELECTOR_LEAF_DEPS) !== null;
}

/** The session's selected leaf as the leaf commands' host; keys and `__odjKeyboard` share it (#13989). */
function _selectedLeafHost(selector: SelectorSession): SelectorLeafHost | null {
	const leafId = selector.selectedLeaf();
	return leafId === null ? null : createSelectorLeafHost(leafId, SELECTOR_LEAF_DEPS);
}

/** The key press a set of shortcut handlers answers: its stamp and where it landed. */
interface ShortcutPress {
	timeStamp: number;
	target: EventTarget | null;
}

/** The /performance shortcut handlers for one press; keys and `__odjKeyboard` share them (#13770). */
function _shortcutHandlers(
	selector: SelectorSession,
	press: ShortcutPress
): PerformanceShortcutHandlers {
	return {
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
		sectionJump: (direction) => {
			void _sectionJump(direction);
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
		},
		selectorCommand: (command) => {
			selector.run(command, {
				timeStamp: press.timeStamp,
				startId: _selectorNodeAt(press.target)
			});
		},
		clearSelector: () => {
			selector.clear();
		},
		selectorLeafCommand: (command) => {
			const selectorLeaf = _selectedLeafHost(selector);
			if (selectorLeaf === null) throw new Error(`${command}: no selector leaf is selected`);
			runCommand(command, { timeStamp: press.timeStamp, selectorLeaf });
		}
	};
}

/** True when a pointer press landed on a deck or mixer channel rather than
 * the library, so Cmd+A means "jump back" instead of Select all. */
function _isDeckPress(target: EventTarget | null): boolean {
	if (!(target instanceof Element)) return false;
	return (
		target.closest('[data-deck-hover]') !== null || target.closest('[data-mixer-channel]') !== null
	);
}

/** #12286: the one live install's teardown, so a second install replaces it rather than stacking. */
let uninstallLive: (() => void) | null = null;

/** Install the /performance window shortcuts, replacing any live install; returns its teardown. */
export function installPerformanceHotkeys(): () => void {
	uninstallLive?.();
	let deckFocus = false;
	const selector = createSelectorSession({
		snapshot: snapshotSelectorNodes,
		version: selectorRegistryVersion,
		showRing: setSelectorRingTarget,
		hideRing: clearSelectorRingTarget
	});
	const onPointer = (e: PointerEvent): void => {
		deckFocus = _isDeckPress(e.target);
	};
	// #13720: existing bindings win; the sampler takes digits and PageUp /
	// PageDown only while an edge is awake (sampler-keys.ts), never [ or ].
	function tryEdgeSamplerHotkey(e: KeyboardEvent): boolean {
		if (!uiPrefs.sampler_enabled || nativeShellKind() !== 'electron') return false;
		const action = samplerKeyAction(e, {
			inTextField: isTextEntryTarget(e.target),
			awakeEdge: awakeSamplerEdge(
				{ L: sampler.edges.L.awake, R: sampler.edges.R.awake },
				sampler.lastEdge
			)
		});
		if (action === null) return false;
		if (action.kind === 'flip') flipSet(action.edge, action.delta);
		else if (action.kind === 'fire') fireEdgeTab(action.edge, action.index);
		e.preventDefault();
		return true;
	}

	const onKey = (e: KeyboardEvent): void => {
		// #14604: a key typed inside a modal dialog is the dialog's, not the page's.
		if (isModalDialogTarget(e.target)) return;
		// LIBM-82b: an open type-ahead prefix on a track row owns every printable key.
		if (trackTypeAhead.ownsKey(e)) return;
		if (tryEdgeSamplerHotkey(e)) return;
		const handlers = _shortcutHandlers(selector, { timeStamp: e.timeStamp, target: e.target });
		if (handleKeymapKeydown(e, handlers)) return;
		if (!legacyPerformanceChordsActive(activeKeyboardPreset())) return;
		handlePerformanceShortcutKeydown(e, handlers, {
			settingsOpen: isSettingsOpen(),
			gridJump: gridJumpEnabled(),
			deckFocus,
			libraryFocus: e.target instanceof Element && e.target.closest('[data-library-root]') !== null,
			dialSelected: selectedKnobIds().length > 0,
			selectorActive: selector.selected() !== null,
			selectorInLibrary: selector.selectionWithin(selId.section('library')),
			selectorLeafId: selector.selectedLeaf(),
			selectorLeafAdjustable: _leafAdjustable
		});
	};
	window.addEventListener('pointerdown', onPointer, { capture: true });
	window.addEventListener('keydown', onKey, { capture: true });
	// #13770: `__odjKeyboard.run` drives this same session and these same handlers.
	const selectorHost = {
		...selector.commandHost(() => performance.now()),
		selectedLeaf: () => _selectedLeafHost(selector)
	};
	setKeyboardSelectorProvider(() => selectorHost);
	setKeyboardShortcutHandlersProvider(() =>
		_shortcutHandlers(selector, { timeStamp: performance.now(), target: null })
	);
	// #12256: a layout change that removes the selected node moves the selection and ring at once.
	const stopReconcile = onSelectorRegistryChange(() => selector.reconcile());
	// #13428: a re-render that drops the focused control hands focus back by selector identity.
	const stopFocusRestore = installSelectorFocusRestore();
	let uninstalled = false;
	// #12286: idempotent, so a replaced install's late teardown cannot clear the live install's providers.
	const uninstall = (): void => {
		if (uninstalled) return;
		uninstalled = true;
		stopFocusRestore();
		stopReconcile();
		window.removeEventListener('pointerdown', onPointer, { capture: true });
		window.removeEventListener('keydown', onKey, { capture: true });
		setKeyboardSelectorProvider(null);
		setKeyboardShortcutHandlersProvider(null);
		selector.clear();
		if (uninstallLive === uninstall) uninstallLive = null;
	};
	uninstallLive = uninstall;
	return uninstall;
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
