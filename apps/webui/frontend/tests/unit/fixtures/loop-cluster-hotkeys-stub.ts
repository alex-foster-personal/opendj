/**
 * Stand-in for `$lib/rb/performance-hotkeys` in LoopCluster SSR tests.
 *
 * The real module installs a window keydown listener and imports the audio
 * engine and the performance IPC; the loop cluster only calls these three
 * hover/interaction notes from click handlers, which an SSR render never runs.
 */
import type { DeckId } from '$lib/rb/deck-slots';

export type { DeckId };

export function noteLoopInteraction(_deck: DeckId): void {}

export function armLoopHover(_deck: DeckId): void {}

export function clearLoopHover(_deck: DeckId): void {}
