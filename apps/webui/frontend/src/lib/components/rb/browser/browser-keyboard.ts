/**
 * Browser keyboard navigation (IOPIN-01) for BrowserPanel's window keydown.
 *
 * Arrows and W/S move the selection inside the focused zone, Left/Right and
 * A/D move focus across playlist -> tracks -> deck target, Enter acts on the
 * focused zone, and a double Enter on the same track row loads it the way a
 * row double-click does. Cmd/Ctrl+F (Shift for the whole collection) opens
 * the browser search. The focus zone and deck target are application state
 * owned here, not incidental DOM focus; BrowserPanel supplies the pane and
 * search accessors it does not own.
 */

import { tick } from 'svelte';
import type { DeckId } from '$lib/rb/deck-id';
import {
	browserNavigationMayHandle,
	browserSelectionDelta,
	moveBrowserFocus,
	type BrowserFocusZone
} from '$lib/rb/browser-navigation';

export type BrowserSearchModeRequest = 'filter' | 'find' | 'collection';

export interface BrowserKeyboardDeps {
	/** The selected track id in the active pane, and that pane's index. */
	selectedId(): string | null;
	activePane(): number;
	/** Move the track selection one row (the MIDI browse path). */
	moveTrackSelection(delta: -1 | 1): void;
	searchFocused(): boolean;
	searchMode(): BrowserSearchModeRequest;
	openSearchMode(mode: BrowserSearchModeRequest): void;
}

export interface BrowserKeyboard {
	onKey(e: KeyboardEvent): void;
	/** Forget a pending first Enter (selection moved). */
	resetEnter(): void;
	/** A track-selection move: forget the first Enter and own the tracks zone. */
	tracksMoved(): void;
}

/** True when the key belongs to the target (text field, slider, dialog,
 * menu, or a button that is not a browser deck/playlist target). */
export function editableTarget(target: EventTarget | null): boolean {
	if (!(target instanceof HTMLElement)) return false;
	const interactive = target.closest('button, a, [role="button"]');
	if (interactive !== null && !interactive.matches(
		'.deck-target, [data-testid="playlist-row"], [data-testid="playlist-all-tracks"]'
	)) return true;
	return (
		target.closest('[role="slider"], [role="dialog"], dialog, [role="menu"]') !== null ||
		target.tagName === 'INPUT' ||
		target.tagName === 'TEXTAREA' ||
		target.tagName === 'SELECT' ||
		target.isContentEditable
	);
}

function movePlaylistSelection(delta: -1 | 1): void {
	if (typeof document === 'undefined') return;
	const nodes = Array.from(document.querySelectorAll<HTMLElement>('[data-testid="playlist-all-tracks"], [data-testid="playlist-row"]'));
	if (nodes.length === 0) return;
	const active = document.activeElement;
	const current = active instanceof HTMLElement ? nodes.indexOf(active) : -1;
	const next = current < 0 ? (delta > 0 ? 0 : nodes.length - 1) : Math.max(0, Math.min(nodes.length - 1, current + delta));
	nodes[next].click();
	nodes[next].focus();
}

export function createBrowserKeyboard(deps: BrowserKeyboardDeps): BrowserKeyboard {
	let browserFocus: BrowserFocusZone = 'tracks';
	let browserDeckTarget: DeckId = 1;
	let lastTrackEnter: { at: number; stableId: string | null; pane: number } | null = null;

	function selectedTrackElement(): HTMLElement | null {
		const id = deps.selectedId();
		if (id === null || typeof document === 'undefined') return null;
		return document.querySelector<HTMLElement>(`[data-testid="track-row"][data-stable-id="${CSS.escape(id)}"]`);
	}

	function focusBrowserZone(): void {
		void tick().then(() => {
			if (typeof document === 'undefined') return;
			if (browserFocus === 'tracks') {
				selectedTrackElement()?.focus();
				return;
			}
			if (browserFocus === 'playlist') {
				document.querySelector<HTMLElement>('[data-testid="playlist-row"].selected, [data-testid="playlist-all-tracks"].selected')?.focus();
				return;
			}
			const targets = selectedTrackElement()?.querySelectorAll<HTMLButtonElement>('button.deck-target');
			targets?.[browserDeckTarget - 1]?.focus();
		});
	}

	function activateFocusedDeckTarget(): void {
		const row = selectedTrackElement();
		if (row === null) return;
		const targets = Array.from(row.querySelectorAll<HTMLButtonElement>('button.deck-target'));
		targets[browserDeckTarget - 1]?.click();
	}

	function triggerTrackDoubleEnter(): void {
		const row = selectedTrackElement();
		if (row === null) return;
		// Route through TrackTable's existing double-click handler so its deck
		// reservation and master-deck safeguards stay the one implementation.
		row.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, cancelable: true }));
	}

	function onKey(e: KeyboardEvent): void {
		if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === 'f' || e.key === 'F')) {
			// Don't steal Cmd/Ctrl+F from text fields outside the browser search box.
			const t = e.target;
			if (t instanceof HTMLElement && !t.closest('.rb-search') && editableTarget(t)) return;
			e.preventDefault();
			if (e.shiftKey) deps.openSearchMode('collection');
			else if (deps.searchFocused() && deps.searchMode() === 'filter') deps.openSearchMode('find');
			else deps.openSearchMode('filter');
			return;
		}

		if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
		if (!browserNavigationMayHandle({
			editable: editableTarget(e.target),
			contextMenuOpen: document.querySelector('[data-testid="context-menu"]') !== null
		})) return;

		const delta = browserSelectionDelta(e.key);
		if (delta !== null) {
			lastTrackEnter = null;
			e.preventDefault();
			if (browserFocus === 'playlist') movePlaylistSelection(delta);
			else if (browserFocus === 'deck') {
				browserDeckTarget = Math.max(1, Math.min(4, browserDeckTarget + delta)) as DeckId;
				focusBrowserZone();
			}
			else {
				browserFocus = 'tracks';
				deps.moveTrackSelection(delta);
				focusBrowserZone();
			}
			return;
		}
		const horizontal = e.key === 'a' || e.key === 'A' ? 'ArrowLeft'
			: e.key === 'd' || e.key === 'D' ? 'ArrowRight' : e.key;
		if (horizontal === 'ArrowLeft' || horizontal === 'ArrowRight') {
			lastTrackEnter = null;
			e.preventDefault();
			browserFocus = moveBrowserFocus(browserFocus, horizontal);
			focusBrowserZone();
			return;
		}
		if (e.key !== 'Enter') return;
		// A directly focused playlist row already handles Enter in PlaylistTree;
		// a native deck button activates itself. Neither is a track-row
		// double-Enter, regardless of an earlier browserFocus state.
		const target = e.target instanceof HTMLElement ? e.target : null;
		if (target?.closest('[data-testid="playlist-row"], [data-testid="playlist-all-tracks"]')) {
			lastTrackEnter = null;
			browserFocus = 'playlist';
			return;
		}
		const deckTarget = target?.closest<HTMLButtonElement>('button.deck-target');
		if (deckTarget) {
			lastTrackEnter = null;
			browserFocus = 'deck';
			const targets = Array.from(deckTarget.parentElement?.querySelectorAll('button.deck-target') ?? []);
			const index = targets.indexOf(deckTarget);
			if (index >= 0) browserDeckTarget = (index + 1) as DeckId;
			return;
		}
		e.preventDefault();
		if (browserFocus === 'playlist') {
			(document.activeElement instanceof HTMLElement ? document.activeElement : null)?.click();
			return;
		}
		if (browserFocus === 'deck') {
			activateFocusedDeckTarget();
			return;
		}
		const now = performance.now();
		const stableId = deps.selectedId();
		const pane = deps.activePane();
		if (lastTrackEnter !== null && now - lastTrackEnter.at <= 500 &&
			lastTrackEnter.stableId === stableId && lastTrackEnter.pane === pane) {
			lastTrackEnter = null;
			triggerTrackDoubleEnter();
		} else {
			lastTrackEnter = { at: now, stableId, pane };
			focusBrowserZone();
		}
	}

	return {
		onKey,
		resetEnter: () => {
			lastTrackEnter = null;
		},
		tracksMoved: () => {
			lastTrackEnter = null;
			browserFocus = 'tracks';
		}
	};
}
