import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import RecordInputPickerView, { MASTER_VALUE, NONE_VALUE } from './RecordInputPickerView.svelte';

/**
 * The REC button's source picker (SET-10 / SET-12): MACHINE records Open DJ's
 * own master mix, LOOPBACK lists loopback inputs and links an installer, MIDI
 * lists the interface inputs a hardware mixer returns on. "Tracklist only" is
 * always reachable. The container lists inputs, remembers the last one and
 * starts the recorder. Device names and paths are invented.
 */
const modes = [
	{ mode: 'machine', label: 'MACHINE', title: "Record Open DJ's own master mix: no driver needed" },
	{ mode: 'loopback', label: 'LOOPBACK', title: 'Record a loopback input such as BlackHole' },
	{
		mode: 'midi',
		label: 'MIDI',
		title: 'A hardware mixer or controller: record the audio interface input its mix comes back on'
	}
] as const;

const meta = {
	title: 'Components/Browser/RecordInputPickerView',
	component: RecordInputPickerView,
	tags: ['autodocs'],
	args: {
		modes,
		mode: 'machine',
		selected: MASTER_VALUE,
		loading: false,
		busy: false,
		devicesLoaded: true,
		listed: [],
		listError: null,
		rememberError: null,
		masterUnavailable: null,
		recordingsDir: '/Users/dev/Music/Open DJ/Recordings',
		onswitchmode: fn(),
		oninstall: fn(),
		onstart: fn(),
		oncancel: fn()
	},
	parameters: { layout: 'fullscreen' }
} satisfies Meta<typeof RecordInputPickerView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** MACHINE: the master mix, with what it captures and where it is saved. */
export const Machine: Story = {};

/** MACHINE where the engine cannot tap the master mix. */
export const MachineUnavailable: Story = {
	args: {
		selected: null,
		masterUnavailable:
			'The Rust engine (preview) mixes outside the page, so its master mix cannot be recorded yet. Switch Settings > Audio engine to Web Audio, or record a loopback input.'
	}
};

/** LOOPBACK with one installed loopback input selected. */
export const Loopback: Story = {
	args: {
		mode: 'loopback',
		selected: 'Example Loopback 2ch',
		listed: [{ index: 3, name: 'Example Loopback 2ch', loopback: true }]
	}
};

/** LOOPBACK on a machine with none installed: the note and the installer link. */
export const NoLoopback: Story = { args: { mode: 'loopback', selected: NONE_VALUE } };

/** MIDI: interface inputs, nothing picked yet, so Start is disabled. */
export const Midi: Story = {
	args: {
		mode: 'midi',
		selected: null,
		listed: [
			{ index: 0, name: 'Built-in Microphone', loopback: false },
			{ index: 1, name: 'Example Audio Interface In 1-2', loopback: false }
		]
	}
};

/** The input listing and the remembered source both failed. */
export const Errors: Story = {
	args: {
		mode: 'midi',
		selected: NONE_VALUE,
		devicesLoaded: false,
		listError: 'recorder_unavailable',
		rememberError: 'prefs read failed'
	}
};

/** Still finding inputs. */
export const Loading: Story = { args: { loading: true, selected: null } };
