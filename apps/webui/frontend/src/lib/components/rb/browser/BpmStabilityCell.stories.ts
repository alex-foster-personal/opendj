import type { Meta, StoryObj } from '@storybook/sveltekit';

import BpmStabilityCell from './BpmStabilityCell.svelte';

/**
 * The library BPM Stability cell (issue #7879). Presentational: it renders the
 * daemon's served `grid_quality.tempo` text and tone verbatim and never fetches
 * or computes. Each story is one verdict the scan can store.
 */
const meta = {
	title: 'Components/Browser/BpmStabilityCell',
	component: BpmStabilityCell,
	tags: ['autodocs']
} satisfies Meta<typeof BpmStabilityCell>;

export default meta;
type Story = StoryObj<typeof meta>;

const OK = 'Beatgrid: all 632 beat intervals are within 3 ms of the median (evenly spaced; not checked against the audio)';
const grid = (tempo: Record<string, unknown> | null, over: Record<string, unknown> = {}) =>
	({ grid_class: 'ok', reason: null, dismissed: false, message: null, tempo, ...over }) as never;

export const Steady: Story = {
	args: {
		grid: grid({ verdict: 'steady', largest_change_pct: 0, change_count: 0, text: 'steady', tone: 'none', title: `Tempo holds steady for the whole track. ${OK}` })
	}
};

export const SmallDrift: Story = {
	args: {
		grid: grid({ verdict: 'drift', largest_change_pct: 0.6, change_count: 0, text: 'drift <1%', tone: 'amber', title: `Tempo drifts. ${OK}` })
	}
};

export const DeepRedChanges: Story = {
	args: {
		grid: grid(
			{ verdict: 'changes', largest_change_pct: 18, change_count: 3, text: '3× change', tone: 'deep_red', title: 'Tempo changes.' },
			{ grid_class: 'variable_tempo', message: 'Beatgrid: tempo changes through this track' }
		)
	}
};

export const SuspectGrid: Story = {
	args: {
		grid: grid(
			{ verdict: 'drift', largest_change_pct: 3.2, change_count: 0, text: 'drift ±3%', tone: 'amber', title: 'Tempo drifts.' },
			{ grid_class: 'suspect', message: 'Beatgrid: 300 of 600 beat intervals are uneven' }
		)
	}
};

/** Never judged: the cell is blank, never "steady". */
export const NotAnalyzed: Story = { args: { grid: grid(null) } };
