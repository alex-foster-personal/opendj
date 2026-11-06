import type { Meta, StoryObj } from '@storybook/sveltekit';

import type { ComponentProps } from 'svelte';

import { WAVE_WINDOW_S } from './wave-window';
import LyricLanesView from './LyricLanesView.svelte';

/**
 * The lyric overlay switch for one wave row: aligned words win when the
 * container hands them over, otherwise the synced line lane renders. A word
 * fetch failure shows WORDS ERROR one row above the line lane's own error,
 * so the two never overlap. The lanes are absolutely positioned, so the
 * decorator gives the story root the size and backing of a deck row. Words,
 * lines and timings are invented.
 */
// The prop's own type, so the story adds no importer to api-karaoke.ts.
type Word = NonNullable<ComponentProps<typeof LyricLanesView>['words']>[number];

const TEXT = ['Hold', 'the', 'light', 'above', 'the', 'water', 'line', 'we', 'drift', 'home'];
const words: Word[] = TEXT.map((word, idx) => ({
	idx,
	word,
	start_s: 10 + idx * 0.8,
	end_s: 10.6 + idx * 0.8,
	line_final: idx === 6 || idx === TEXT.length - 1,
	witness: 'agree'
}));
const lyrics = {
	lines: [
		{ start_ms: 10_000, text: 'Hold the light above the water line' },
		{ start_ms: 15_600, text: 'We drift home' },
		{ start_ms: 21_000, text: 'Hold the light' }
	]
};

const meta = {
	title: 'Components/Wave/LyricLanesView',
	component: LyricLanesView,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			const root =
				typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) {
				root.style.position = 'relative';
				root.style.width = '720px';
				root.style.height = '56px';
				root.style.background = 'var(--rb-bg)';
			}
			return story();
		}
	],
	args: {
		wordsError: null,
		words,
		lyrics,
		loadError: null,
		positionMs: 12_500,
		pitch: 1,
		windowSeconds: WAVE_WINDOW_S,
		playheadFrac: 0.5
	}
} satisfies Meta<typeof LyricLanesView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Word overlay on and the track has aligned words: the word lane paints. */
export const WordLane: Story = {};

/** No words (overlay off, or none aligned): the synced line lane paints instead. */
export const LineLane: Story = { args: { words: null } };

/** The word fetch failed: WORDS ERROR above the line lane, reason on hover. */
export const WordsError: Story = {
	args: { words: null, wordsError: 'karaoke words request failed: 500' }
};

/** Both fetches failed: the two error labels stack without overlapping. */
export const BothErrors: Story = {
	args: {
		words: null,
		lyrics: null,
		wordsError: 'karaoke words request failed: 500',
		loadError: new Error('lyrics service returned 503')
	}
};
