import { MIDI_CROSS_PATH, MIDI_TICK_PATH, type MidiLabelGlyphKind } from '$lib/ui/icon-glyphs';

import './MidiStatusGlyph.react.css';

// React twin of MidiStatusGlyph.svelte: same DOM, held to MidiStatusGlyph.contract.ts.
export type MidiStatusGlyphProps = { glyph: MidiLabelGlyphKind };

export default function MidiStatusGlyph({ glyph }: MidiStatusGlyphProps) {
	if (glyph === 'none') return null;
	return (
		<svg className="midi-glyph" data-midi-glyph={glyph} viewBox="0 0 12 12" width="9" height="9" aria-hidden="true">
			<path d={glyph === 'tick' ? MIDI_TICK_PATH : MIDI_CROSS_PATH} fill="none" stroke="currentColor" strokeWidth="1.6" />
		</svg>
	);
}
