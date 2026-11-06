import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';

import WaveGutterView from './WaveGutterView.svelte';

/**
 * The waveform-row gutter (DECKUX-40): the deck number large in the brand
 * face, then the track summary. The summary reads live key and artwork in
 * the app, so here it is a plain stand-in through the `summary` snippet
 * (WaveTrackSummaryView has its own stories). An empty deck shows only its
 * number, at the border token's weight. The decorator gives the root a deck
 * row's height.
 */
const summary = createRawSnippet(() => ({
	render: () =>
		`<span style="align-self:center;font-size:var(--rb-fs-label);color:var(--rb-text)">Night Drive (Extended Mix)</span>`
}));

const meta = {
	title: 'Components/Wave/WaveGutterView',
	component: WaveGutterView,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			const root =
				typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) {
				root.style.display = 'flex';
				root.style.height = 'var(--rb-waverow-h, 40px)';
			}
			return story();
		}
	],
	args: { deckId: 1, loaded: true, summary }
} satisfies Meta<typeof WaveGutterView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A loaded deck: number, then the summary. */
export const Loaded: Story = {};

/** An empty deck: only its number, dimmed; hover says no track is loaded. */
export const Empty: Story = { args: { loaded: false } };

/** Deck 3 loaded, as on a four-deck layout. */
export const Deck3: Story = { args: { deckId: 3 } };
