import type { Meta, StoryObj } from '@storybook/sveltekit';
import ArtworkDeckNumber from './ArtworkDeckNumber.svelte';

// ARTNUM-9649b/10287: the deck number outline over a deck's artwork square. The overlay
// is absolutely positioned, so each story's canvas is the square it covers.
const meta = {
	title: 'Components/ArtworkDeckNumber',
	component: ArtworkDeckNumber,
	tags: ['autodocs'],
	args: { deck: 1, mode: 'halo', sizePx: 67, stableId: null, playing: false },
	argTypes: {
		mode: { control: 'inline-radio', options: ['off', 'halo', 'glow', 'tinted'] },
		deck: { control: 'inline-radio', options: [1, 2, 3, 4] }
	}
} satisfies Meta<typeof ArtworkDeckNumber>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Deck header square. */
export const Header: Story = {};
/** Waveform gutter (Gothic and Light rows). */
export const Gutter: Story = { args: { deck: 2, sizePx: 56 } };
/** Mixer column art, the smallest size that carries it. */
export const Mixer: Story = { args: { deck: 3, sizePx: 40 } };
/** The short-window 22px gutter skips the overlay. */
export const ShortGutterSkipped: Story = { args: { deck: 4, sizePx: 22 } };
/** Default: the pref is off, nothing draws. */
export const Off: Story = { args: { mode: 'off' } };
/** ARTNUM-11719: halo plus an outer glow in the art's colour (fixed tint here). */
export const Glow: Story = { args: { mode: 'glow', tint: { r: 200, g: 30, b: 30 } } };
/** ARTNUM-11719: halo shaded from and glow in the art's colour; stroke stays white. */
export const Tinted: Story = { args: { mode: 'tinted', tint: { r: 200, g: 30, b: 30 } } };
