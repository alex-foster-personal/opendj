/**
 * Pure right-click target classifier for Quick Draw (#9450).
 */
import { animateTargetOfKnobId, ANIMATE_CONTROLS, type AnimateControl } from '../rb/animate-expr';
import { STEM_CONTROL_IDS, type StemControl } from '../rb/stem-types';
import type { CueSlot, DeckNumber, MenuTarget } from './registry';

const DECK_IDS = [1, 2, 3, 4] as const;
export type DeckId = (typeof DECK_IDS)[number];

export const CUE_SLOTS: readonly CueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

export interface ElementLike {
	closest(selector: string): ElementLike | null;
	getAttribute(name: string): string | null;
	contains(other: ElementLike): boolean;
}

export type QuickDrawTarget =
	| Extract<MenuTarget, { kind: 'hot-cue' | 'mixer-channel' | 'waveform' | 'deck' | 'stem' }>
	| { kind: 'track-chip'; stableId: string }
	| {
			kind: 'mixer-control';
			label: string;
			deck: DeckId | null;
			control: AnimateControl;
			channel: DeckId | null;
	  }
	| { kind: 'tree-readonly' | 'library-source' | 'perf-empty' };

function deckAttr(el: ElementLike | null, attr: string): DeckId | null {
	const n = Number(el?.getAttribute(attr));
	return DECK_IDS.includes(n as DeckId) ? (n as DeckId) : null;
}

export function deckFromTarget(t: ElementLike | null): DeckId | null {
	const el = t?.closest('[data-deck]') ?? null;
	return deckAttr(el, 'data-deck');
}

export function stableIdFromTarget(t: ElementLike | null): string | null {
	const el = t?.closest('[data-stable-id]') ?? null;
	if (el === null) return null;
	const id = el.getAttribute('data-stable-id');
	return id !== null && id !== '' ? id : null;
}

function stemFromAttr(value: string | null): StemControl | null {
	if (value === null) return null;
	return (STEM_CONTROL_IDS as readonly string[]).includes(value) ? (value as StemControl) : null;
}

export function targetOf(t: ElementLike | null, knownStableId: string | null): QuickDrawTarget {
	if (knownStableId !== null) return { kind: 'track-chip', stableId: knownStableId };
	if (t === null) return { kind: 'perf-empty' };
	const cue = t.closest('[data-hot-cue-slot]');
	const cueDeck = deckAttr(cue?.closest('[data-deck]') ?? null, 'data-deck');
	const slot = cue?.getAttribute('data-hot-cue-slot') as CueSlot | null | undefined;
	if (cueDeck !== null && slot != null && CUE_SLOTS.includes(slot)) {
		return { kind: 'hot-cue', deck: cueDeck, slot };
	}
	const mixerDeck = deckAttr(t.closest('[data-mixer-channel]'), 'data-mixer-channel');
	const dialEl = t.closest('[data-knob-id], [data-animate-control]');
	const dial = dialEl?.getAttribute('data-animate-control') ?? dialEl?.getAttribute('data-knob-id');
	const animated = dial == null ? null : animateTargetOfKnobId(dial);
	if (animated !== null) {
		return {
			kind: 'mixer-control',
			label: ANIMATE_CONTROLS[animated.control].label,
			...animated,
			channel: mixerDeck
		};
	}
	if (mixerDeck !== null) return { kind: 'mixer-channel', deck: mixerDeck };
	const lane = t.closest('[data-stem]');
	const stem = stemFromAttr(lane?.getAttribute('data-stem') ?? null);
	const stemDeck = deckAttr(lane?.closest('[data-deck]') ?? null, 'data-deck');
	if (stem !== null && stemDeck !== null) {
		return { kind: 'stem', deck: stemDeck as DeckNumber, stem };
	}
	const markedEl = t.closest('[data-menu-target]');
	const marked = markedEl?.getAttribute('data-menu-target');
	const stableEl = t.closest('[data-stable-id]');
	if (
		(marked === 'tree-readonly' || marked === 'library-source') &&
		(stableEl === null || markedEl!.contains(stableEl))
	) {
		return { kind: marked };
	}
	const stableId = stableIdFromTarget(t);
	if (stableId !== null) return { kind: 'track-chip', stableId };
	const deck = deckFromTarget(t);
	if (deck !== null) {
		return t.closest('[data-wave-surface]') !== null
			? { kind: 'waveform', deck }
			: { kind: 'deck', deck };
	}
	return { kind: 'perf-empty' };
}
