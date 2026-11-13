/**
 * Keyboard selector ids for the waveform lanes and the library panes (GitHub issue #13703, epic #9372).
 *
 * Each deck's waveform lane is a leaf `waveforms.deck<n>` under the waveforms section, and the
 * library's playlist tree, search box and track table are leaves `library.tree`,
 * `library.search` and `library.tracks` under the library section. Ids are plain strings
 * composed from `selId.section`, so `selector-register.ts` gains no new helper.
 * The edge sampler on/off chip (#13722) is mounted inside the waveform stack, so it is the
 * leaf `waveforms.sampler` beside the lanes; it sits right of deck 1's lane top edge, so a
 * drill-in still lands on deck 1 and moves down still walk the lanes (#13703).
 */
import { selId } from '$lib/keyboard/selector-register';
import type { DeckId } from '$lib/rb/deck-slots';
import type { DeckLayoutMode } from '$lib/rb/deck-layout-prefs';

export type LibraryPane = 'tree' | 'search' | 'tracks';

/** Returns the selector leaf id of one deck's waveform lane. */
export function waveLaneSelectorId(deck: DeckId): string {
	return `${selId.section('waveforms')}.deck${deck}`;
}

/** Returns whether a deck's waveform lane is live: two-deck (LESS) layout hides lanes 3 and 4. */
export function waveLaneSelectable(deck: DeckId, layout: DeckLayoutMode): boolean {
	return !(layout === 'less' && (deck === 3 || deck === 4));
}

/** Returns the selector leaf id of the waveform area's edge sampler on/off chip. */
export function waveSamplerSelectorId(): string {
	return `${selId.section('waveforms')}.sampler`;
}

/** Returns the selector leaf id of one library pane. */
export function libraryPaneSelectorId(pane: LibraryPane): string {
	return `${selId.section('library')}.${pane}`;
}
