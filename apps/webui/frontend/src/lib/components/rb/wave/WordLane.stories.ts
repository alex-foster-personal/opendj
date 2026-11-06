import type { Meta, StoryObj } from '@storybook/sveltekit';

import type { ComponentProps } from 'svelte';

import { WAVE_WINDOW_S } from './wave-window';
import WordLane from './WordLane.svelte';

/**
 * The karaoke word lane over one deck waveform: words placed by the
 * word-lanes.ts packer across the 24s window centered on the playhead, the
 * word under the playhead bold, and ASR-suspect words (contradict / lost)
 * dimmer and underlined. The lane is absolutely positioned and measures its
 * own width, so the decorator gives the story root the size and backing of a
 * deck row. Words and timings are invented.
 */
// The prop's own type, so the story adds no importer to api-karaoke.ts.
type Word = ComponentProps<typeof WordLane>['words'][number];

const TEXT = ['Hold', 'the', 'light', 'above', 'the', 'water', 'line', 'we', 'drift', 'home', 'tonight', 'again'];
const words = (witness: (idx: number) => string | null = () => 'agree'): Word[] =>
	TEXT.map((word, idx) => ({
		idx,
		word,
		start_s: 10 + idx * 0.8,
		end_s: 10.6 + idx * 0.8,
		line_final: idx === 5 || idx === TEXT.length - 1,
		witness: witness(idx)
	}));

const meta = {
	title: 'Components/Wave/WordLane',
	component: WordLane,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			const root = typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) {
				root.style.position = 'relative';
				root.style.width = '720px';
				root.style.height = '56px';
				root.style.background = 'var(--rb-bg)';
			}
			return story();
		}
	],
	args: { words: words(), positionMs: 13_300, pitch: 1, windowSeconds: WAVE_WINDOW_S, playheadFrac: 0.5 }
} satisfies Meta<typeof WordLane>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Mid-line: the word under the playhead is bold, the rest alternate rows. */
export const Typical: Story = {};

/** Two words the ASR witness contradicted or lost: shown dimmer and underlined, never hidden. */
export const SuspectWords: Story = {
	args: { words: words((idx) => (idx === 2 ? 'contradict' : idx === 7 ? 'lost' : 'agree')) }
};

/** Pitch fader at +8%: the window covers more track time, so words sit closer together. */
export const PitchedUp: Story = { args: { pitch: 1.08 } };

/** Playhead well before the first word: the words wait on the right of the lane. */
export const BeforeFirstWord: Story = { args: { positionMs: 2_000 } };

/** No aligned words: an empty lane over the waveform. */
export const NoWords: Story = { args: { words: [] }, tags: ['renders-nothing'] };
