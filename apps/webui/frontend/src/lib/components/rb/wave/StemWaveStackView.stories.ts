import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';

import StemWaveStackView from './StemWaveStackView.svelte';

/**
 * The column of per-stem strips under a deck waveform, one per stem control
 * in a fixed order. Each strip fetches its own envelope in the app, so the
 * story passes a plain labeled bar per stem through the `row` snippet.
 */
const row = createRawSnippet((stem: () => string) => ({
	render: () =>
		`<div style="height:12px;line-height:12px;padding:0 6px;font-size:8px;letter-spacing:0.08em;color:var(--rb-text-dim);border-top:1px solid var(--rb-border)">${stem().toUpperCase()}</div>`
}));

const meta = {
	title: 'Components/Wave/StemWaveStackView',
	component: StemWaveStackView,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			const root =
				typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) root.style.width = '600px';
			return story();
		}
	],
	args: { deckId: 1, visible: true, row }
} satisfies Meta<typeof StemWaveStackView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Stems shown with a track loaded: one strip per stem control. */
export const Shown: Story = {};

/** Stems hidden, or no track loaded: nothing is drawn. */
export const Hidden: Story = { args: { visible: false }, tags: ['renders-nothing'] };
