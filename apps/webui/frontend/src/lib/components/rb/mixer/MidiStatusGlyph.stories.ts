import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './MidiStatusGlyph.contract';
import MidiStatusGlyph from './MidiStatusGlyph.svelte';

/**
 * Status mark beside a MIDI entry label (CHROME-07). The glyph kind comes
 * from the app's midiLabelGlyph() derivation; this component only draws it.
 * It inherits `currentColor`, so the wrapper sets a visible text color.
 */
const meta = {
	title: 'Contracts/Mixer/MidiStatusGlyph',
	component: MidiStatusGlyph,
	tags: ['autodocs'],
	argTypes: {
		glyph: { control: 'inline-radio', options: ['tick', 'cross', 'none'] }
	}
} satisfies Meta<typeof MidiStatusGlyph>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Tick: Story = contracts.Tick;
export const Cross: Story = contracts.Cross;
export const None: Story = contracts.None;
