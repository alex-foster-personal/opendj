import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, within } from 'storybook/test';

import { contracts } from './PerfMeterSpark.contract';
import PerfMeterSpark from './PerfMeterSpark.svelte';

/** Presentational sparkline. A null sample is a gap (dashed join), never plotted as zero. */
const meta = {
	title: 'Contracts/Root/PerfMeterSpark',
	component: PerfMeterSpark,
	tags: ['autodocs']
} satisfies Meta<typeof PerfMeterSpark>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Labeled: Story = contracts.Labeled;
export const Bare: Story = contracts.Bare;
export const EmptyTitle: Story = contracts.EmptyTitle;

export const Rising: Story = {
	args: {
		label: 'Frame ms',
		title: 'Frame time in ms over the last 10 samples',
		values: [4, 5, 5, 6, 8, 9, 11, 12, 14, 15]
	},
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTitle('Frame time in ms over the last 10 samples')).toHaveTextContent('Frame ms');
	}
};

/** Nulls bridge with a dashed segment. */
export const WithGaps: Story = {
	args: {
		label: 'Decode ms',
		title: 'Decode time in ms; gaps are samples that were not taken',
		values: [10, 12, null, null, 9, 11, null, 13]
	},
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTitle('Decode time in ms; gaps are samples that were not taken')).toHaveTextContent('Decode ms');
		await expect(canvasElement.querySelector('svg.perf-spark-svg')).not.toBeNull();
	}
};

/** Flat series: the scale pads so the line sits mid-chart. */
export const Flat: Story = {
	args: { label: 'RAM MB', values: [200, 200, 200, 200, 200] },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.perf-spark-label')).toHaveTextContent('RAM MB');
		await expect(canvasElement.querySelectorAll('.perf-spark-svg polyline')).not.toHaveLength(0);
	}
};

/** No label: used beside a row that already names the metric. */
export const NoLabel: Story = {
	args: { values: [3, 2, 4, 3, 5, 2] },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.perf-spark-label')).toBeNull();
		await expect(canvasElement.querySelectorAll('.perf-spark-svg polyline')).not.toHaveLength(0);
	}
};

/** Nothing measured yet: an empty chart. */
export const Empty: Story = {
	args: { label: 'Frame ms', values: [] },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.perf-spark-label')).toHaveTextContent('Frame ms');
		await expect(canvasElement.querySelectorAll('.perf-spark-svg polyline')).toHaveLength(0);
	}
};

export const AllGaps: Story = {
	args: { label: 'Frame ms', values: [null, null, null] },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.perf-spark-label')).toHaveTextContent('Frame ms');
		await expect(canvasElement.querySelectorAll('.perf-spark-svg polyline')).toHaveLength(0);
	}
};
