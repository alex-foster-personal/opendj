import type { Meta, StoryObj } from '@storybook/sveltekit';

import type { EnrichSummary, LaneCounts } from '$lib/enrich/enrich-card';

import EnrichCardView from './EnrichCardView.svelte';

/**
 * One story per state of the enrich-on-open card (ENRICH-01). The state
 * inventory is specs/state-inventories/library-enrichment.md; each story here
 * is the evidence for one of its cells, and
 * `node scripts/capture-ui-contracts.mjs` turns the stories into the images
 * under specs/ui-contracts/enrich-on-open/states/.
 *
 * Nothing is a mocked network call: EnrichCardView is pure props, and the
 * args have the exact shape of GET /api/v1/enrich/summary. Counts are those
 * of the 1,274-track library the card was first verified on.
 */
const TOTAL = 1274;
const full: LaneCounts = { total: TOTAL, done: TOTAL, missing: 0, failed: 0, declined: 0, unavailable: null };

const summary = (
	lanes: Record<string, Partial<LaneCounts>> = {},
	over: Partial<EnrichSummary> = {}
): EnrichSummary => ({
	show: true,
	analysis: {
		lanes: Object.fromEntries(
			['tags', 'strip', 'beatgrid', 'key', 'loudness', 'waveform'].map((lane) => [
				lane,
				{ ...full, ...(lanes[lane] ?? {}) }
			])
		)
	},
	analysis_error: null,
	coverage: {
		on_disk: TOTAL,
		done: { stems: TOTAL, lyrics: TOTAL },
		terminal: { stems: 0, lyrics: 0 },
		failed: { stems: 0, lyrics: 0 },
		pending: { stems: 0, lyrics: 0 }
	},
	coverage_error: null,
	stems: { state: 'done', pending: 0, reason: null },
	decisions: {},
	...over
});

const lyrics = (found: number, none: number, pending: number, failed = 0): EnrichSummary['coverage'] => ({
	on_disk: TOTAL,
	done: { stems: TOTAL, lyrics: found },
	terminal: { stems: 0, lyrics: none },
	failed: { stems: 0, lyrics: failed },
	pending: { stems: 0, lyrics: pending }
});

const meta = {
	title: 'Components/EnrichCardView',
	component: EnrichCardView,
	tags: ['autodocs']
} satisfies Meta<typeof EnrichCardView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Queued and analyzing: counts with their denominator on hover, nothing to click but Hide. */
export const Working: Story = {
	args: {
		summary: summary({
			beatgrid: { done: 351, missing: 923 },
			key: { done: 44, missing: 1230 },
			loudness: { done: 196, missing: 1078 }
		})
	}
};

/** Failed: red line, reasons on hover, and the card offers Retry. */
export const Failed: Story = {
	args: {
		summary: summary({
			strip: { done: 1271, failed: 3, failed_reasons: { 'the decode finished but produced no strip': 3 } },
			beatgrid: { done: 351, missing: 923 }
		})
	}
};

/** Declined: measured and left blank as low-confidence. Dimmed, counted apart, not a failure. */
export const Declined: Story = {
	args: {
		summary: summary({
			beatgrid: {
				done: 338,
				declined: 13,
				missing: 923,
				declined_reasons: { grid_fit_bar_phase_below_floor: 12, grid_fit_too_few_beats: 1 }
			},
			key: { done: 8, declined: 1, missing: 1265, declined_reasons: { 'no_tonal_center: ambiguous_margin': 1 } }
		})
	}
};

/** Unavailable: this computer cannot produce the lane at all; italic, with the reason, and Retry. */
export const UnavailableOnThisComputer: Story = {
	args: {
		summary: summary({
			loudness: { done: 196, missing: 1078, unavailable: 'the bundled ffmpeg has no astats filter' }
		})
	}
};

/** Lyrics are automatic: found, none available, and still to look up. */
export const LyricsInProgress: Story = {
	args: { summary: summary({}, { coverage: lyrics(243, 603, 428) }) }
};

/** The one opt-in question, with its three answers. */
export const StemsAsked: Story = {
	args: {
		summary: summary({}, { stems: { state: 'ask', pending: 1150, reason: null } })
	}
};

/** No stems source on this computer: said, never asked. */
export const StemsNoSource: Story = {
	args: {
		summary: summary(
			{ key: { done: 44, missing: 1230 } },
			{ stems: { state: 'no_source', pending: 1150, reason: 'no stems farm is configured' } }
		)
	}
};

/** The user answered Never: the stems row is gone, the automatic lanes still report. */
export const StemsUserDeclined: Story = {
	args: {
		summary: summary(
			{ key: { done: 44, missing: 1230 } },
			{ stems: { state: 'user_declined', pending: 1150, reason: null }, decisions: { stems: 'never' } }
		)
	}
};

/** The measurement itself is missing: said in words, never shown as a finished library. */
export const StatusUnknown: Story = {
	args: {
		summary: summary(
			{},
			{
				analysis: null,
				analysis_error: 'the ahead-of-time analysis drain is not running here',
				coverage: null,
				coverage_error: 'OperationalError: database is locked',
				stems: { state: 'unknown', pending: null, reason: 'stems coverage could not be read' }
			}
		)
	}
};

/** The summary request failed outright. */
export const LoadFailed: Story = {
	args: { summary: null, loadError: 'GET /api/v1/enrich/summary failed: 503 Service Unavailable' }
};

/** A button's request failed: the error sits under the buttons, the question stays. */
export const ActionFailed: Story = {
	args: {
		summary: summary({}, { stems: { state: 'ask', pending: 1150, reason: null } }),
		actionError: 'PUT /api/v1/enrich/decisions/stems failed: 500 Internal Server Error'
	}
};

/** Everything at once, as first seen on the real library. */
export const RealLibrary: Story = {
	args: {
		summary: summary(
			{
				strip: { done: 1271, failed: 3, failed_reasons: { 'the decode finished but produced no strip': 3 } },
				beatgrid: { done: 347, declined: 13, missing: 914 },
				key: { done: 8, declined: 1, missing: 1265 },
				loudness: { done: 282, missing: 992 }
			},
			{ coverage: lyrics(256, 632, 386), stems: { state: 'ask', pending: 1150, reason: null } }
		)
	}
};
