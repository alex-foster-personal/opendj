import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, within } from 'storybook/test';

import type { DeckState } from '$lib/rb/deck-state-types';
import type { StemControl, StemControlState, StemDeckState } from '$lib/rb/stem-types';

import { contracts } from './StemRow.contract';
import StemRow from './StemRow.svelte';

/**
 * Pure props: the deck is a hand-built DeckState fixture for an invented
 * track. Click toggles mute and Shift+click toggles solo; the Actions panel
 * records the calls, nothing touches an audio engine.
 */
/** The invented track's title; a constant so no `title:` literal sits above the meta. */
const TRACK_TITLE = 'Night Drive (Extended Mix)';

const ctl = (over: Partial<StemControlState> = {}): StemControlState => ({
	muted: false,
	solo: false,
	gain: 0.5,
	...over
});

const stemState = (over: Partial<StemDeckState> = {}, controls: Partial<Record<StemControl, StemControlState>> = {}): StemDeckState => ({
	status: 'ready',
	source: 'demucs',
	model: 'htdemucs',
	layout: 'demucs4',
	available_controls: ['vocal', 'instrumental', 'drums', 'bass', 'other'],
	alignment: { sample_rate_hz: 44100, frame_count: 10_584_000, channel_count: 2, duration_ms: 240_000 },
	controls: {
		vocal: ctl(),
		instrumental: ctl(),
		drums: ctl(),
		bass: ctl(),
		other: ctl(),
		...controls
	},
	error: null,
	load: null,
	...over
});

const deckWith = (stems: StemDeckState): DeckState =>
	({
		deck_id: 1,
		stable_id: 'a'.repeat(40),
		edit_id: null,
		edit_in_ms: null,
		edit_out_ms: null,
		source_path: '/music/Lumen Fields - Night Drive (Extended Mix).flac',
		title: TRACK_TITLE,
		artist: 'Lumen Fields',
		rating: 4,
		bpm: 128,
		key: '8A',
		key_shift_semitones: 0,
		duration_ms: 240_000,
		position_ms: 60_000,
		playing: false,
		audible: false,
		transport_pending: false,
		cue_ms: null,
		pitch: 1,
		quantize_enabled: true,
		quantize_grid_beats: 1,
		beat_sync_enabled: false,
		key_sync_enabled: false,
		key_sync_mode: 'exact',
		key_sync_target: null,
		master_tempo_enabled: true,
		slip_enabled: false,
		slip_active: false,
		slip_position_ms: null,
		sync_mode: 'bar',
		sync_error: null,
		processor_error: null,
		stems,
		loop: null,
		safety_loop: null,
		pending_loop_in_ms: null,
		last_loop: null,
		hot_cues: [],
		hot_cue_revisions: { A: '', B: '', C: '', D: '', E: '', F: '', G: '', H: '' },
		has_rb_mapping: true,
		anlz: null,
		anlz_error: null,
		last_load_latency_ms: 850,
		load_generation: 1,
		last_load_stages: null,
		is_master: false
	}) as DeckState;

const meta = {
	title: 'Contracts/Deck/StemRow',
	component: StemRow,
	tags: ['autodocs'],
	parameters: { layout: 'padded' },
	argTypes: { pending: { control: 'boolean' }, testIdScope: { control: 'inline-radio', options: ['deck', 'channel'] } },
	args: {
		deck: deckWith(stemState()),
		pending: false,
		testIdScope: 'deck',
		onMute: fn(async () => {}),
		onSolo: fn(async () => {}),
		onLoad: fn(async () => {})
	}
} satisfies Meta<typeof StemRow>;
export default meta;
type Story = StoryObj<typeof meta>;

export const ReadyDemucs: Story = contracts.ReadyDemucs;
export const RoformerChannel: Story = contracts.RoformerChannel;
export const FailedRetry: Story = contracts.FailedRetry;
export const Fetching: Story = contracts.Fetching;
export const Probing: Story = contracts.Probing;
export const Downloading: Story = contracts.Downloading;
export const Decoding: Story = contracts.Decoding;
export const Waiting: Story = contracts.Waiting;
export const Switching: Story = contracts.Switching;
export const NoStemsNoAction: Story = contracts.NoStemsNoAction;
export const NoStemsSeparate: Story = contracts.NoStemsSeparate;
export const NoStemsNoSource: Story = contracts.NoStemsNoSource;
export const NoStemsQueued: Story = contracts.NoStemsQueued;
export const NoStemsFailed: Story = contracts.NoStemsFailed;
export const QuickAca: Story = contracts.QuickAca;
export const QuickInst: Story = contracts.QuickInst;
export const QuickCustom: Story = contracts.QuickCustom;
export const QuickLoading: Story = contracts.QuickLoading;

/** Four-stem Demucs bundle, all live and unmuted. */
export const Ready: Story = {
	play: async ({ canvasElement }) => {
		const group = within(canvasElement).getByRole('group', { name: 'stem controls deck 1' });
		await expect(group).toHaveAttribute('data-stems-status', 'ready');
		await expect(group).toHaveAttribute('data-stems-state', 'ready');
		await expect(canvasElement.querySelectorAll('.chip')).toHaveLength(5);
	}
};

/** Vocals muted and drums soloed. */
export const MutedAndSolo: Story = {
	args: {
		deck: deckWith(stemState({}, { vocal: ctl({ muted: true }), drums: ctl({ solo: true }) }))
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByRole('button', { name: /VOCAL stem mute/ })).toHaveClass('muted');
		await expect(canvas.getByRole('button', { name: /DRUMS stem mute/ })).toHaveClass('solo');
	}
};

/** Two-stem split: DRUMS is folded into INST, so it renders inert with a reason; BASS and HARM are hidden. */
export const TwoStemSplit: Story = {
	args: {
		deck: deckWith(
			stemState({ source: 'roformer', model: 'bs-roformer', layout: 'roformer2', available_controls: ['vocal', 'instrumental'] })
		)
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(canvas.getByRole('button', { name: /DRUMS stem mute/ })).toBeDisabled();
		await expect(canvas.queryByRole('button', { name: /BASS stem mute/ })).toBeNull();
	}
};

/** Mixer-strip variant: labels are cut to three letters and testids use the channel scope. */
export const ChannelScope: Story = {
	args: { testIdScope: 'channel' },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="stem-vocal-channel-1"]')).toHaveTextContent('VOC');
	}
};

/** Secondary load in flight: chips are inert and the status label shows the phase. */
export const Loading: Story = {
	args: {
		deck: deckWith(
			stemState({
				status: 'loading',
				source: null,
				model: null,
				layout: null,
				available_controls: [],
				alignment: null,
				load: { phase: 'decoding', progress: null, reason: null }
			})
		)
	},
	play: async ({ canvasElement }) => {
		const group = within(canvasElement).getByRole('group', { name: 'stem controls deck 1' });
		await expect(group).toHaveAttribute('data-stems-status', 'loading');
		await expect(canvasElement.querySelector('[data-testid="stem-status-deck-1"]')).not.toBeNull();
	}
};

/** Load failed: a retry action appears because the deck passes onLoad. */
export const LoadFailed: Story = {
	args: {
		deck: deckWith(
			stemState({
				status: 'error',
				source: null,
				model: null,
				layout: null,
				available_controls: [],
				alignment: null,
				error: 'stem bundle checksum mismatch'
			})
		)
	},
	play: async ({ canvasElement }) => {
		const group = within(canvasElement).getByRole('group', { name: 'stem controls deck 1' });
		await expect(group).toHaveAttribute('data-stems-status', 'error');
		await expect(canvasElement.querySelector('button[data-testid="stem-status-deck-1"]')).not.toBeNull();
	}
};

/** Failed with no onLoad (mixer strip): the reason is on hover only. */
export const LoadFailedNoRetry: Story = {
	args: {
		onLoad: undefined as never,
		deck: deckWith(
			stemState({
				status: 'error',
				source: null,
				model: null,
				layout: null,
				available_controls: [],
				alignment: null,
				error: 'stem bundle checksum mismatch'
			})
		)
	},
	play: async ({ canvasElement }) => {
		const group = within(canvasElement).getByRole('group', { name: 'stem controls deck 1' });
		await expect(group).toHaveAttribute('data-stems-status', 'error');
		await expect(canvasElement.querySelector('button[data-testid="stem-status-deck-1"]')).toBeNull();
	}
};

/** Track has no stems on this machine or in the cloud. */
export const NoStems: Story = {
	args: {
		deck: deckWith(
			stemState({
				status: 'unavailable',
				source: null,
				model: null,
				layout: null,
				available_controls: [],
				alignment: null,
				error: 'no bundle found'
			})
		)
	},
	play: async ({ canvasElement }) => {
		const group = within(canvasElement).getByRole('group', { name: 'stem controls deck 1' });
		await expect(group).toHaveAttribute('data-stems-status', 'unavailable');
		for (const chip of canvasElement.querySelectorAll('.chip')) await expect(chip).toBeDisabled();
	}
};
