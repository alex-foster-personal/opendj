import type { Meta, StoryObj } from '@storybook/sveltekit';

import AnalysisDots from './AnalysisDots.svelte';

/**
 * Second witness for the numeric-readout rule: the 3x3 dot grid is a purely
 * graphical readout, so the count it encodes ("analysis coverage: 4/9") only
 * exists in the container's hover title. Per-kind detail (pin a2bb92d16f4a /
 * issue #880) lives in AnalysisDotsPopover.svelte's custom hover popover, a
 * separate wrapper component around this one - not a second native title on
 * each dot, which would compete with that popover exactly the way
 * ControlExplainer's own doc comment warns against.
 *
 * This component itself stays presentational (props in, markup out, no
 * jobProgress, no fetch) which is what keeps it eligible for Storybook -
 * storybook-stories.test.mjs enforces exactly that line. The popover, the
 * `stableId`/click-to-queue affordance, and the live jobProgress reads all
 * live in AnalysisDotsPopover.svelte instead, which is deliberately NOT
 * storied (same split PerfMeters.svelte already establishes for anything
 * reaching for jobProgress).
 *
 * In `issues` mode a dot lights ONLY where the backend detected a real
 * problem. Undetected kinds stay dark rather than being coloured green, so
 * the grid never implies a check that was not run.
 */
const meta = {
	title: 'Components/Browser/AnalysisDots',
	component: AnalysisDots,
	tags: ['autodocs'],
	argTypes: {
		mode: { control: 'inline-radio', options: ['coverage', 'issues'] }
	}
} satisfies Meta<typeof AnalysisDots>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Nothing analysed yet: all nine slots dark, hover reads 0/9. */
export const NoCoverage: Story = {
	args: { badge: {}, mode: 'coverage' }
};

/** The common mid-state while jobs are still working through the library. */
export const PartialCoverage: Story = {
	args: {
		badge: { vocals: true, beatgrid: true, key: true, waveform: true },
		mode: 'coverage'
	}
};

/** Fully analysed: the grid gains its outline ring as an at-a-glance tick. */
export const FullCoverage: Story = {
	args: {
		badge: {
			vocals: true,
			beatgrid: true,
			key: true,
			cues: true,
			waveform: true,
			phrase: true,
			loudness: true,
			stems: true,
			lyrics: true
		},
		mode: 'coverage'
	}
};

/** Err column: one warning, one error, everything else deliberately dark. */
export const Issues: Story = {
	args: {
		mode: 'issues',
		issues: {
			beatgrid: { severity: 'warning', detail: 'first downbeat is 40 ms off the transient' },
			loudness: { severity: 'error', detail: 'clipping detected in 3 regions' }
		}
	}
};
