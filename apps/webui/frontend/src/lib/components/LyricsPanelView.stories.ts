import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import LyricsPanelView from './LyricsPanelView.svelte';

type Karaoke = NonNullable<ComponentProps<typeof LyricsPanelView>['data']>;
type Word = Karaoke['words'][number];

/** Invented lyric words; the witness verdicts cover every tint. */
const LINE = [
	['Headlights', 'agree'],
	['on', 'agree'],
	['the', 'unheard'],
	['harbor', 'drift'],
	['road', 'agree'],
	['counting', 'contradict'],
	['every', 'lost'],
	['bridge', 'lost'],
	['we', 'unmatchable'],
	['crossed', null]
] as const;

const words: Word[] = LINE.map(([word, witness], idx) => ({
	idx,
	word,
	witness,
	start_s: 12 + idx * 0.6,
	end_s: 12.5 + idx * 0.6,
	line_final: idx === 4 || idx === LINE.length - 1
}));

const track = (over: Partial<NonNullable<Karaoke['verdict']>> = {}): Karaoke => ({
	available: true,
	words,
	verdict: {
		stable_id: 'a'.repeat(40),
		computed_at: '2026-10-12T21:30:00Z',
		updated_at: '2026-10-12T21:30:00Z',
		pipeline_version: 'v3',
		has_words: true,
		effective: 'vocal',
		verdict: 'vocal',
		effective_verdict: 'vocal',
		coverage_pct: 41.7,
		n_words: words.length,
		n_lines: 2,
		source: 'lrclib:get',
		language_iso3: 'eng',
		pct_witness_red: 0.3,
		override: null,
		...over
	}
});

/**
 * The track page's karaoke lyrics inspector: QC banner, the audio player
 * when the probe said it can play, and the words tinted by the independent
 * ASR witness. The container reads the lyric cache and probes the audio;
 * here audioOk is never true, so no audio request is made. Lyrics are
 * invented.
 */
const meta = {
	title: 'Components/LyricsPanelView',
	component: LyricsPanelView,
	tags: ['autodocs'],
	args: {
		data: track(),
		loaded: true,
		failure: null,
		audioOk: false,
		audioReason: "the daemon answered HTTP 404 for this track's audio",
		audioUrl: '',
		onAudioError: fn(),
		setOverride: fn()
	}
} satisfies Meta<typeof LyricsPanelView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A vocal track whose audio file cannot be resolved: the reason is stated. */
export const VocalAudioMissing: Story = {};

/** A human override is set: the badge shows and a clear button appears. */
export const Overridden: Story = {
	args: { data: track({ effective_verdict: 'sparse', override: 'sparse', coverage_pct: 6.2 }) }
};

/** No coverage measure, no line count, no source: the n/a forms. */
export const Sparse: Story = {
	args: {
		data: track({
			effective_verdict: 'no-lyrics',
			coverage_pct: null,
			n_lines: null,
			source: null,
			language_iso3: null,
			pct_witness_red: null
		})
	}
};

/** Audio not probed yet: no player and no reason. */
export const AudioProbing: Story = { args: { audioOk: null, audioReason: null } };

/** The cache entry is still loading. */
export const Loading: Story = { args: { loaded: false, data: null } };

/** The lyric read failed. */
export const ReadFailed: Story = { args: { data: null, failure: '500 Internal Server Error' } };

/** Nothing from the pipeline yet. */
export const NoData: Story = { args: { data: null } };
