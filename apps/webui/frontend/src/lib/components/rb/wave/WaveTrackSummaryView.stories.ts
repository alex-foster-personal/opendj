import type { Meta, StoryObj } from '@storybook/sveltekit';

import { camelotKeyColor } from '$lib/rb/camelot-color';

import WaveTrackSummaryView from './WaveTrackSummaryView.svelte';

/**
 * Track identity in a waveform-row gutter (DECKUX-40): square artwork the
 * full row height, then the title (Mixed In Key prefix stripped, fading at
 * the edge, scrubbing on hover) over the live key and BPM. Missing artwork
 * is empty space with the reason on hover. The decorator sizes the root like
 * the gutter beside a deck number. The artwork is an inline SVG data URI, so
 * no story requests anything; the key color comes from the app's own
 * camelotKeyColor. Titles, keys and tempos are invented.
 */
const ART =
	'data:image/svg+xml,' +
	encodeURIComponent(
		'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3a6ea5"/><stop offset="1" stop-color="#c0587e"/></linearGradient></defs><rect width="40" height="40" fill="url(#g)"/><circle cx="20" cy="20" r="9" fill="none" stroke="#fff" stroke-opacity="0.6" stroke-width="2"/></svg>'
	);

const meta = {
	title: 'Components/Wave/WaveTrackSummaryView',
	component: WaveTrackSummaryView,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			const root =
				typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) {
				root.style.display = 'flex';
				root.style.width = '126px';
				root.style.height = '40px';
			}
			return story();
		}
	],
	args: {
		fullTitle: 'Night Drive (Extended Mix)',
		artworkSrc: ART,
		liveKey: '8A',
		keyColor: camelotKeyColor('8A'),
		liveBpm: 124.0,
		onArtworkLoad: () => {},
		onArtworkError: () => {}
	}
} satisfies Meta<typeof WaveTrackSummaryView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Artwork, title, and the live key in its Camelot color beside the BPM. */
export const Typical: Story = {};

/** A Mixed In Key prefixed title: the gutter drops "9A - 7 - " and keeps the full name on hover. */
export const MixedInKeyPrefix: Story = { args: { fullTitle: '9A - 7 - Lantern Drift' } };

/** A title far wider than the gutter: it fades at the edge and scrubs through on hover. */
export const LongTitle: Story = {
	args: { fullTitle: 'Late Night Peak Hour Rolling Basslines (Extended Archive Mix)' }
};

/** No artwork anywhere: empty space, the reason on hover. */
export const NoArtwork: Story = { args: { artworkSrc: null } };

/** No beatgrid tempo or key yet: only the title shows. */
export const NoKeyOrTempo: Story = { args: { liveKey: null, keyColor: null, liveBpm: null } };

/** Pitched up: the BPM readout is the grid tempo times the pitch fader, to one decimal. */
export const Pitched: Story = {
	args: { liveBpm: 131.9, liveKey: '11B', keyColor: camelotKeyColor('11B') }
};
