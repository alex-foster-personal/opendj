import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';

import type { LyricsLine, LyricsTrack, LyricsWord, LyricLineBand } from '$lib/rb/lyrics/types';

import DeckLyricLine from './DeckLyricLine.svelte';

/**
 * Lyrics are invented and the position comes from a plain function, the same
 * seam the deck uses for the engine's presented clock. Word timings are in
 * source-track seconds. No daemon, no fetch.
 */
function buildTrack(
	lines: { text: string; band: LyricLineBand }[],
	verdict: LyricsTrack['verdict'] = 'vocal'
): LyricsTrack {
	const words: LyricsWord[] = [];
	const outLines: LyricsLine[] = [];
	let t = 10;
	lines.forEach((ln, index) => {
		const first = words.length;
		const start = t;
		for (const w of ln.text.split(/\s+/)) {
			words.push({ word: w, start_s: t, end_s: t + 0.4, score: -0.2, line_final: false, src_idx: words.length });
			t += 0.5;
		}
		outLines.push({
			index,
			text: ln.text,
			first_word: first,
			last_word: words.length - 1,
			start_s: start,
			end_s: t - 0.1,
			fidelity: ln.band === 'good' ? 'word' : 'line',
			band: ln.band,
			quality: ln.band === 'unjudged' ? null : 0.9,
			n_red: ln.band === 'bad' ? 3 : 0,
			n_judged: ln.band === 'unjudged' ? 0 : 6
		});
		t += 1.5;
	});
	const counts = { good: 0, uncertain: 0, bad: 0, unjudged: 0 };
	for (const l of outLines) counts[l.band] += 1;
	return {
		id: 'lumen-fields-night-drive',
		artist: 'Lumen Fields',
		title: 'Night Drive (Extended Mix)',
		language_iso: 'en',
		duration_s: t + 20,
		words,
		lines: outLines,
		verdict,
		word_fidelity_lines: counts.good,
		band_counts: counts
	};
}

const goodTrack = buildTrack([
	{ text: 'headlights paint the empty road', band: 'good' },
	{ text: 'we keep the radio low', band: 'good' },
	{ text: 'every mile a quiet promise', band: 'good' }
]);

const baseArgs: ComponentProps<typeof DeckLyricLine> = {
	track: goodTrack,
	entryState: 'loaded',
	error: null,
	positionSource: () => 11,
	rows: 2
};

const meta = {
	title: 'Components/Deck/DeckLyricLine',
	component: DeckLyricLine,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	// In the deck the strip sits in a sized flex column, and its sung line takes
	// the free height (flex: 1 1 0). With no sized parent that line collapses to
	// 0 px and clips its words away, so the root stands in for the deck slot.
	decorators: [
		(story, context) => {
			const root = typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) {
				root.style.display = 'flex';
				root.style.flexDirection = 'column';
				root.style.width = '360px';
				root.style.height = `${(context.args.rows ?? 2) * 22 + 6}px`;
			}
			return story();
		}
	],
	args: baseArgs,
	argTypes: { rows: { control: 'inline-radio', options: [1, 2, 3] } }
} satisfies Meta<typeof DeckLyricLine>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Playhead inside the first line: per-word wipe on a witness-GOOD line, with a preview row. */
export const WordWipe: Story = {};

/** One row only: the compact strip with no upcoming-line preview. */
export const CompactOneRow: Story = { args: { rows: 1 } };

/** Three rows: current line plus two upcoming preview lines. */
export const ThreeRows: Story = { args: { rows: 3, positionSource: () => 11 } };

/** Before the first sung word: the VOCAL IN countdown. */
export const PrerollCountdown: Story = { args: { positionSource: () => 4 } };

/** After the last sung word: VOCAL DONE. */
export const Outro: Story = { args: { positionSource: () => 120 } };

/** Witness band UNCERTAIN: whole line lit, dimmed, with a ~ prefix. */
export const UncertainBand: Story = {
	args: {
		track: buildTrack([
			{ text: 'static in the amber light', band: 'uncertain' },
			{ text: 'I lose the thread again', band: 'uncertain' }
		]),
		positionSource: () => 11
	}
};

/** Witness band BAD: the whole line lights amber and never animates per word. */
export const BadBand: Story = {
	args: {
		track: buildTrack([
			{ text: 'static in the amber light', band: 'bad' },
			{ text: 'I lose the thread again', band: 'bad' }
		]),
		positionSource: () => 11
	}
};

/** A very long line: the fit shrinks and wraps, paging only as a last resort. */
export const LongLine: Story = {
	args: {
		track: buildTrack([
			{
				text: 'under the neon rain the city hums a song we only half remember while the last train fades beyond the river and the morning slowly learns our names',
				band: 'good'
			},
			{ text: 'short follow up', band: 'good' }
		]),
		positionSource: () => 12
	}
};

/** lyrics-cache is still fetching. */
export const Loading: Story = { args: { track: null, entryState: 'loading' } };

/** The pipeline has no lyric data for this track. */
export const NoData: Story = { args: { track: null, entryState: 'none' } };

/** Calibrated verdict says instrumental: words exist but are not shown. */
export const InstrumentalVerdict: Story = {
	args: { track: buildTrack([{ text: 'la la la', band: 'good' }], 'no-lyrics') }
};

/** Fetch or adapter failure, rendered as a real failure. */
export const ErrorState: Story = {
	args: { track: null, entryState: 'error', error: 'lyrics request timed out after 10s' }
};
