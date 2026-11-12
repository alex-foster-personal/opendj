/**
 * Serato DJ Pro keyboard defaults (deck 1 and 2 only). Serato Ctrl -> Cmd in chord space.
 *
 * Clean room: published default key table reproduced as data with Open DJ command ids.
 * Source: the-drop.serato.com/how-to/serato-dj-keyboard-shortcuts/
 *
 * Intentionally unbound: Q/A reverse (no reverse command), Ctrl+F search (library owns Cmd+F),
 * Ctrl+L locate (no locate command).
 */
import type { Keymap } from '../keymap';

function merge(
	d1: Record<string, readonly string[]>,
	d2: Record<string, readonly string[]>,
	global: Record<string, readonly string[]>
): Record<string, readonly string[]> {
	return { ...d1, ...d2, ...global };
}

export const SERATO_KEYMAP: Keymap = {
	id: 'serato',
	label: 'Serato DJ Pro',
	bindings: merge(
		{
			'deck.1.play': ['W'],
			'deck.1.cue': ['I'],
			'deck.1.tempo_down': ['T'],
			'deck.1.tempo_up': ['Y'],
			'deck.1.keylock': ['F5'],
			'deck.1.reloop_exit': ['['],
			'deck.1.loop_in': ['O'],
			'deck.1.loop_out': ['P'],
			'deck.1.hot_cue.A': ['1'],
			'deck.1.hot_cue.B': ['2'],
			'deck.1.hot_cue.C': ['3'],
			'deck.1.hot_cue.D': ['4'],
			'deck.1.hot_cue.E': ['5'],
			'deck.1.hot_cue_set.A': ['Cmd+1'],
			'deck.1.hot_cue_set.B': ['Cmd+2'],
			'deck.1.hot_cue_set.C': ['Cmd+3'],
			'deck.1.hot_cue_set.D': ['Cmd+4'],
			'deck.1.hot_cue_set.E': ['Cmd+5'],
			'deck.1.hot_cue_clear.A': ['Cmd+Shift+1'],
			'deck.1.hot_cue_clear.B': ['Cmd+Shift+2'],
			'deck.1.hot_cue_clear.C': ['Cmd+Shift+3'],
			'deck.1.hot_cue_clear.D': ['Cmd+Shift+4'],
			'deck.1.hot_cue_clear.E': ['Cmd+Shift+5'],
			'deck.1.load': ['Shift+ArrowLeft'],
			'deck.1.instant_double': ['Cmd+Shift+ArrowLeft']
		},
		{
			'deck.2.play': ['S'],
			'deck.2.cue': ['K'],
			'deck.2.tempo_down': ['G'],
			'deck.2.tempo_up': ['H'],
			'deck.2.keylock': ['F10'],
			'deck.2.reloop_exit': ["'"],
			'deck.2.loop_in': ['L'],
			'deck.2.loop_out': [';'],
			'deck.2.hot_cue.A': ['6'],
			'deck.2.hot_cue.B': ['7'],
			'deck.2.hot_cue.C': ['8'],
			'deck.2.hot_cue.D': ['9'],
			'deck.2.hot_cue.E': ['0'],
			'deck.2.hot_cue_set.A': ['Cmd+6'],
			'deck.2.hot_cue_set.B': ['Cmd+7'],
			'deck.2.hot_cue_set.C': ['Cmd+8'],
			'deck.2.hot_cue_set.D': ['Cmd+9'],
			'deck.2.hot_cue_set.E': ['Cmd+0'],
			'deck.2.hot_cue_clear.A': ['Cmd+Shift+6'],
			'deck.2.hot_cue_clear.B': ['Cmd+Shift+7'],
			'deck.2.hot_cue_clear.C': ['Cmd+Shift+8'],
			'deck.2.hot_cue_clear.D': ['Cmd+Shift+9'],
			'deck.2.hot_cue_clear.E': ['Cmd+Shift+0'],
			'deck.2.load': ['Shift+ArrowRight'],
			'deck.2.instant_double': ['Cmd+Shift+ArrowRight']
		},
		{
			'wave.zoom_out': ['-'],
			'wave.zoom_in': ['Shift+=']
		}
	)
};
