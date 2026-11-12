/**
 * rekordbox Performance mode keyboard defaults (deck 1 and 2 only).
 *
 * Clean room: published default key table reproduced as data with Open DJ command ids.
 * Sources: reklawdbox.com/reference/keyboard-shortcuts/,
 * deejayplaza.com/en/articles/rekordbox-keyboard-shortcuts.
 *
 * Intentionally unbound (no Open DJ command): Cmd+Z undo load, Cmd+L locate,
 * Space browser-layout toggle.
 */
import type { Keymap } from '../keymap';

function d1d2(
	d1: Record<string, readonly string[]>,
	d2: Record<string, readonly string[]>
): Record<string, readonly string[]> {
	const out: Record<string, readonly string[]> = { ...d1 };
	for (const [id, chords] of Object.entries(d2)) out[id] = chords;
	return out;
}

export const REKORDBOX_KEYMAP: Keymap = {
	id: 'rekordbox',
	label: 'rekordbox',
	bindings: d1d2(
		{
			'deck.1.play': ['Z'],
			'deck.1.cue': ['A'],
			'deck.1.jump_start': ['Shift+A'],
			'deck.1.beat_sync': ['Q'],
			'deck.1.sync_master': ['Shift+Q'],
			'deck.1.hot_cue.A': ['1'],
			'deck.1.hot_cue.B': ['2'],
			'deck.1.hot_cue.C': ['3'],
			'deck.1.hot_cue.D': ['4'],
			'deck.1.hot_cue_clear.A': ['Cmd+Shift+1'],
			'deck.1.hot_cue_clear.B': ['Cmd+Shift+2'],
			'deck.1.hot_cue_clear.C': ['Cmd+Shift+3'],
			'deck.1.hot_cue_clear.D': ['Cmd+Shift+4'],
			'deck.1.loop_in': ['D'],
			'deck.1.loop_out': ['F'],
			'deck.1.reloop_exit': ['G'],
			'deck.1.loop_halve': ['Shift+D'],
			'deck.1.loop_double': ['Shift+G'],
			'deck.1.load': ['Shift+ArrowLeft'],
			'deck.1.instant_double': ['Cmd+Shift+ArrowLeft']
		},
		{
			'deck.2.play': ['N'],
			'deck.2.cue': ['H'],
			'deck.2.jump_start': ['Shift+H'],
			'deck.2.beat_sync': ['Y'],
			'deck.2.sync_master': ['Shift+Y'],
			'deck.2.hot_cue.A': ['6'],
			'deck.2.hot_cue.B': ['7'],
			'deck.2.hot_cue.C': ['8'],
			'deck.2.hot_cue.D': ['9'],
			'deck.2.hot_cue_clear.A': ['Cmd+Shift+6'],
			'deck.2.hot_cue_clear.B': ['Cmd+Shift+7'],
			'deck.2.hot_cue_clear.C': ['Cmd+Shift+8'],
			'deck.2.hot_cue_clear.D': ['Cmd+Shift+9'],
			'deck.2.loop_in': ['K'],
			'deck.2.loop_out': ['L'],
			'deck.2.reloop_exit': [';'],
			'deck.2.loop_halve': ['Shift+K'],
			'deck.2.loop_double': ['Shift+;'],
			'deck.2.load': ['Shift+ArrowRight'],
			'deck.2.instant_double': ['Cmd+Shift+ArrowRight'],
			'wave.zoom_out': ['-'],
			'wave.zoom_in': ['Shift+-']
		}
	)
};
