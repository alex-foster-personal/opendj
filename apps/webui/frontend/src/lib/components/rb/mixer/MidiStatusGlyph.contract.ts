import { expect } from 'storybook/test';

import { MIDI_CROSS_PATH, MIDI_TICK_PATH, type MidiLabelGlyphKind } from '$lib/ui/icon-glyphs';

/**
 * MidiStatusGlyph UI contract, shared by the Svelte story (MidiStatusGlyph.stories.ts)
 * and its React twin (MidiStatusGlyph.stories.tsx). See src/react/README.md.
 */

type Args = { glyph: MidiLabelGlyphKind };
type Ctx = { canvasElement: HTMLElement; args: Args };

const glyph = (root: HTMLElement): SVGSVGElement => {
	const el = root.querySelector<SVGSVGElement>('svg.midi-glyph');
	if (el === null) throw new Error('svg.midi-glyph not rendered');
	return el;
};

export const contracts = {
	/** A bound controller: the tick, tagged data-midi-glyph="tick". */
	Tick: {
		args: { glyph: 'tick' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = glyph(canvasElement);
			await expect(el).toHaveAttribute('data-midi-glyph', 'tick');
			await expect(el).toHaveAttribute('aria-hidden', 'true');
			await expect(el).toHaveAttribute('width', '9');
			await expect(el.querySelector('path')).toHaveAttribute('d', MIDI_TICK_PATH);
			await expect(el.querySelector('path')).toHaveAttribute('stroke-width', '1.6');
		}
	},
	/** MIDI on with nothing bound: the cross. */
	Cross: {
		args: { glyph: 'cross' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = glyph(canvasElement);
			await expect(el).toHaveAttribute('data-midi-glyph', 'cross');
			await expect(el.querySelector('path')).toHaveAttribute('d', MIDI_CROSS_PATH);
		}
	},
	/** Otherwise: no mark at all. */
	None: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { glyph: 'none' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(canvasElement.querySelector('svg')).toBeNull();
		}
	}
};
