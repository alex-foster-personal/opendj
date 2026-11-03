import type { Meta, StoryObj } from '@storybook/sveltekit';

import LineLyricsPanelView from './LineLyricsPanelView.svelte';

/**
 * The track page's line-synced lyrics list. The container fetches the
 * lyrics; these args are its three states. Lines are invented.
 */
const lines = [
	{ start_ms: 12_480, text: 'Headlights on the harbor road' },
	{ start_ms: 16_020, text: 'Counting every bridge we crossed' },
	{ start_ms: 19_755, text: 'Hold the line until the morning' },
	{ start_ms: 19_755, text: 'Hold the line until the morning' },
	{ start_ms: 63_007, text: 'Night drive, night drive' }
];

const meta = {
	title: 'Components/LineLyricsPanelView',
	component: LineLyricsPanelView,
	tags: ['autodocs'],
	args: { lyrics: null, loading: false, error: null }
} satisfies Meta<typeof LineLyricsPanelView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Lyrics from LRCLIB, including a duet line repeated at one stamp. */
export const Lrclib: Story = { args: { lyrics: { source: 'lrclib', lines } } };

/** Lyrics transcribed by the local ASR lane: the header reads 'transcribed'. */
export const Transcribed: Story = { args: { lyrics: { source: 'asr', lines: lines.slice(0, 2) } } };

/** Fetch still in flight. */
export const Loading: Story = { args: { loading: true } };

/** The fetch failed: its message in the error color. */
export const LoadError: Story = { args: { error: 'lyrics request failed: 503 Service Unavailable' } };

/** A lyrics record with no lines renders nothing. */
export const NoLines: Story = {
	tags: ['renders-nothing'],
	args: { lyrics: { source: 'lrclib', lines: [] } }
};
