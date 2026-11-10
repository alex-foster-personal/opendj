import type { Meta, StoryObj } from '@storybook/sveltekit';

import StemWaveRowView from './StemWaveRowView.svelte';

/**
 * One stem's strip under a deck waveform. A stem that cannot be shown is a
 * dimmed, inert placeholder with the reason in its hover title; a ready stem
 * is a blank canvas that StemWaveRow paints from the fetched envelope, so
 * the story shows the empty strip and its backing, never an invented
 * waveform. A live row is a click target: click solos the stem, Shift+click
 * mutes it. The decorator gives the root a deck row's width.
 */
const meta = {
	title: 'Components/Wave/StemWaveRowView',
	component: StemWaveRowView,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			const root =
				typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) root.style.width = '600px';
			return story();
		}
	],
	args: {
		stem: 'drums',
		visible: true,
		unavailableTip: null,
		deckId: 1,
		controlState: { muted: false, solo: false },
		onSolo: () => {},
		onMute: () => {}
	}
} satisfies Meta<typeof StemWaveRowView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Ready: the canvas strip StemWaveRow paints into (blank until an envelope lands). */
export const Ready: Story = {};

/** Stem missing from a two-stem bundle: the dim placeholder, reason on hover. */
export const NotSeparate: Story = {
	args: {
		unavailableTip:
			'DRUMS is not a separate stem in this roformer2 bundle - it is mixed into INST, so it cannot be muted on its own'
	}
};

/** Stems not ready for the loaded track: every strip is the placeholder. */
export const StemsNotReady: Story = {
	args: { stem: 'vocal', unavailableTip: 'stems missing: no aligned artifact' }
};

/** Soloed: the row's aria-pressed is true; a click un-solos, Shift+click mutes. */
export const Soloed: Story = {
	args: { stem: 'vocal', controlState: { muted: false, solo: true } }
};

/** Muted: Shift+click unmutes; painting the muted state is a sibling issue. */
export const Muted: Story = { args: { stem: 'drums', controlState: { muted: true, solo: false } } };

/** Stems hidden, or no track loaded: nothing is drawn. */
export const Hidden: Story = { args: { visible: false }, tags: ['renders-nothing'] };
