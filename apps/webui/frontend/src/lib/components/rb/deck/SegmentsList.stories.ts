import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, within } from 'storybook/test';

import type { AnlzBeat } from '$lib/rb/anlz-types';
import type { SectionSpan } from '$lib/components/rb/wave/sections';

import SegmentsList from './SegmentsList.svelte';

/**
 * STRUCT-9657: the Segments tab of the HOT CUE dropdown on an invented
 * 6-section track at 128 BPM (one bar = 1.875 s). The playhead sits in the
 * Drop, so that row is highlighted.
 */
const BAR_S = (4 * 60) / 128;
const beats: AnlzBeat[] = Array.from({ length: 96 * 4 }, (_, i) => ({
	n: (i % 4) + 1,
	bpm: 128,
	t: (i * 60) / 128
}));
const span = (
	label: string,
	family: SectionSpan['family'],
	startBar: number,
	bars: number
): SectionSpan => ({
	start_s: startBar * BAR_S,
	end_s: (startBar + bars) * BAR_S,
	label,
	family,
	ambient: false,
	labelWidthPx: -1
});
const spans: SectionSpan[] = [
	span('Intro', 'edge', 0, 16),
	span('Verse', 'body', 16, 16),
	span('Build', 'rise', 32, 8),
	span('Drop', 'drop', 40, 16),
	span('Breakdown', 'calm', 56, 16),
	span('Outro', 'edge', 72, 16)
];

const meta = {
	title: 'Components/Deck/SegmentsList',
	component: SegmentsList,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	args: {
		spans,
		beats,
		positionMs: Math.round(44 * BAR_S * 1000),
		hotCues: [],
		onSeek: fn(),
		onSaveHotCue: fn()
	}
} satisfies Meta<typeof SegmentsList>;
export default meta;
type Story = StoryObj<typeof meta>;

/** Six sections, the Drop highlighted as the playing row. */
export const SixSections: Story = {
	play: async ({ canvasElement }) => {
		const rows = canvasElement.querySelectorAll('.segment-row');
		await expect(rows).toHaveLength(6);
		await expect(rows[3]).toHaveClass('playing');
		await expect(within(rows[3] as HTMLElement).getByText('bar 41')).toBeTruthy();
	}
};

/** A track with no sections: a plain message, not an error. */
export const NoSections: Story = {
	args: { spans: [] },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByText('No sections for this track')).toBeTruthy();
	}
};
