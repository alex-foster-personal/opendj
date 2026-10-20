import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './MidiStatusGlyph.contract';
import MidiStatusGlyph from './MidiStatusGlyph.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Mixer/MidiStatusGlyph', component: MidiStatusGlyph } satisfies Meta<typeof MidiStatusGlyph>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Tick: Story = contracts.Tick;
export const Cross: Story = contracts.Cross;
export const None: Story = contracts.None;
