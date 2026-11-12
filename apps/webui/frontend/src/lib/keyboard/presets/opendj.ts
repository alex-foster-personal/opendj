import type { Keymap } from '../keymap';

/** Open DJ default chords (today's performance shortcut routing). */
export const OPENDJ_KEYMAP: Keymap = {
	id: 'opendj',
	label: 'Open DJ',
	bindings: {
		'transport.toggle': ['Space'],
		'transport.toggle_quantized': ['Cmd+Space'],
		'loop.double_last': ['=', 'Shift+='],
		'loop.halve_last': ['-', 'Shift+-'],
		'loop.exit_last': ['Shift+0'],
		'wave.zoom_out': ['['],
		'wave.zoom_in': [']'],
		'comment.pin': ['M'],
		'layout.deck_less': ['Cmd+2'],
		'layout.deck_more': ['Cmd+4'],
		'grid.jump_back_4': ['Cmd+ArrowLeft'],
		'grid.jump_forward_4': ['Cmd+ArrowRight'],
		'grid.jump_back_8': ['Cmd+Shift+ArrowLeft'],
		'grid.jump_forward_8': ['Cmd+Shift+ArrowRight']
	}
};
