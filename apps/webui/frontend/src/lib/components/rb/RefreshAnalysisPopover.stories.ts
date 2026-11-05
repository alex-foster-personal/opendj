import type { Meta, StoryObj } from '@storybook/sveltekit';

import type {
	AnalysisQueue,
	IngestConfig,
	IngestCoverage,
	RefreshStatus
} from '$lib/rb/api-ingest';

import RefreshAnalysisPopover from './RefreshAnalysisPopover.svelte';

/**
 * The hover card behind the TopBar refresh-analysis button. Everything it
 * shows arrives as props (the button owns the polling), so the args ARE the
 * contract. Here `wrapEl` is undefined, so the card is not anchored to a
 * trigger and renders at its static position instead of floating beside one.
 * All figures are invented; each number line carries its own hover title.
 */
const status = (over: Partial<RefreshStatus> = {}): RefreshStatus => ({
	running: false,
	phase: 'idle',
	steps: ['analysis', 'stems', 'vocals'],
	current_step: null,
	step_done: 0,
	step_total: 0,
	steps_completed: [],
	started_at: null,
	finished_at: null,
	error: null,
	log_tail: [],
	recently_done_ids: [],
	...over
});

const coverage: IngestCoverage = {
	total_tracks: 1240,
	on_disk: 1180,
	unreachable: 12,
	missing: { analysis: 37, stems: 214, vocals: 214 },
	corrupt: {},
	availability: { present: 1180, 'awaiting-volume': 48 },
	done: { analysis: 1143, stems: 966, vocals: 966 },
	terminal: {},
	failed: {},
	pending: { analysis: 37, stems: 214, vocals: 214 },
	waiting_on_stems: 0,
	stems_source_refusal: null,
	local: { stems: 966 },
	in_cloud: { stems: 0 },
	awaiting_stem_download: 0,
	stems_index: { state: 'ok', reason: null },
	generated_at: 1791900000,
	age_s: 0,
	refreshing: false,
	refresh_error: null
};

const config: IngestConfig = {
	path: '/music/.opendj/ingest.json',
	steps: [
		{
			id: 'analysis',
			label: 'Analysis',
			enabled: true,
			default_enabled: true,
			requires_rb_row: false,
			hint: 'tempo, key, beatgrid'
		},
		{
			id: 'stems',
			label: 'Stems',
			enabled: true,
			default_enabled: false,
			requires_rb_row: false,
			hint: 'vocal, drums, instrumental'
		},
		{
			id: 'vocals',
			label: 'Vocals',
			enabled: false,
			default_enabled: false,
			requires_rb_row: false,
			hint: 'vocal cache'
		}
	]
};

const queue = (over: Partial<AnalysisQueue> = {}): AnalysisQueue => ({
	unmapped: 40,
	pending: 25,
	analyzed: 12,
	unreachable: 3,
	signature: 'q-0001',
	items: [],
	job: status(),
	auto: {
		enabled: true,
		interval_s: 60,
		attempts: 0,
		last_started_at: null,
		last_signature: null,
		last_outcome: null
	},
	...over
});

const meta = {
	title: 'Components/RefreshAnalysisPopover',
	component: RefreshAnalysisPopover,
	tags: ['autodocs'],
	args: {
		wrapEl: undefined,
		status: status(),
		coverage,
		config,
		queue: queue(),
		fetchError: null,
		clickFeedback: null
	}
} satisfies Meta<typeof RefreshAnalysisPopover>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Nothing running: coverage, queue and enabled steps, plus the idle hint. */
export const Idle: Story = {};

/** A run in flight: phase line, progress bar (title carries done/total) and the log tail. */
export const Running: Story = {
	args: {
		status: status({
			running: true,
			phase: 'running',
			current_step: 'stems',
			step_done: 96,
			step_total: 214,
			steps_completed: ['analysis'],
			log_tail: [
				'analysis: 37/37 done',
				'stems: started',
				'stems: Night Drive (Extended Mix) ok',
				'stems: Low Tide ok'
			]
		})
	}
};

/** The hover fetch failed: only the error line shows under the title. */
export const FetchError: Story = {
	args: {
		fetchError: 'could not reach the daemon (connection refused)',
		status: null,
		coverage: null,
		config: null,
		queue: null
	}
};

/** First paint before any fetch lands: nothing loaded, so only the idle hint renders. */
export const Loading: Story = {
	args: { status: null, coverage: null, config: null, queue: null }
};

/** The latest click's outcome leads, and replaces the idle hint. */
export const ClickFeedback: Story = {
	args: { clickFeedback: 'queued 3 steps over 251 missing tracks' }
};

/** No step enabled and the auto loop off. */
export const NothingEnabled: Story = {
	args: {
		config: { ...config, steps: config.steps.map((s) => ({ ...s, enabled: false })) },
		queue: queue({ auto: { ...queue().auto, enabled: false } })
	}
};
