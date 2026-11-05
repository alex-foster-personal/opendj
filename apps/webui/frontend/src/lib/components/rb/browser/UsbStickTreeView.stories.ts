import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { PlaylistNode as Node } from '$lib/rb/library-types';

import UsbStickTreeView from './UsbStickTreeView.svelte';

/**
 * One plugged-in stick's tree in the USBs tab (USBPLAY-05): All tracks, the
 * stick's playlists and folders in rekordbox order, then History. The
 * container reads the stick and owns folder toggles; this draws the stick
 * view it is handed. Names and counts are invented.
 */
const list = (playlist_id: string, name: string, track_count: number): Node => ({
	playlist_id,
	name,
	track_count,
	broken_count: 0,
	kind: 'usb',
	children: []
});

const tree: Node[] = [
	list('usb:all', 'All tracks', 412),
	{
		playlist_id: 'usb:f1',
		name: 'Gigs',
		track_count: 2,
		broken_count: 0,
		kind: 'folder',
		children: [list('usb:p1', 'Friday Club', 64), list('usb:p2', 'Rooftop', 38)]
	},
	list('usb:p3', 'Backup Bangers', 120),
	list('usb:history', 'History', 87)
];

const meta = {
	title: 'Components/Browser/UsbStickTreeView',
	component: UsbStickTreeView,
	tags: ['autodocs'],
	args: {
		volumeName: 'DJ STICK',
		view: { status: 'ready', name: 'DJ STICK', tree, trackCount: 412, readMs: 180 },
		openFolders: {},
		errorHeadline: '',
		selectedId: null,
		onactivate: fn(),
		onrowkeydown: fn()
	},
	parameters: { layout: 'padded' }
} satisfies Meta<typeof UsbStickTreeView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Read, with the Gigs folder collapsed. */
export const Ready: Story = {};

/** The Gigs folder open and one of its lists selected. */
export const FolderOpen: Story = {
	args: { openFolders: { 'usb:f1': true }, selectedId: 'usb:p1' }
};

/** The stick is being read. */
export const Reading: Story = { args: { view: { status: 'loading' } } };

/** The read failed: the headline shows, the full message is in the hover title. */
export const Error: Story = {
	args: {
		view: {
			status: 'error',
			code: 'USB_STICK_NOT_MOUNTED',
			message: 'the volume is no longer mounted'
		},
		errorHeadline: 'Stick removed'
	}
};
