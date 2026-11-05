import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';
import { createRawSnippet } from 'svelte';

import type { ComponentProps } from 'svelte';

import UsbSourceListView from './UsbSourceListView.svelte';

/**
 * The USBs tab body: one row per plugged-in stick with its details and
 * Import buttons, the macOS removable-volume permission states, and an open
 * stick's tree under its row. The tree reads the stick itself, so it comes in
 * as the stickTree snippet; here it is a labelled placeholder. Stick names
 * and paths are invented.
 */
type Volume = ComponentProps<typeof UsbSourceListView>['volumes'][number];

const stick = (id: string, name: string, over: Partial<Volume> = {}): Volume => ({
	id,
	name,
	mount_path: `/Volumes/${name}`,
	is_music: true,
	present: true,
	first_seen: 1760000000000,
	last_seen: 1760400000000,
	access: 'ok',
	...over
});

const stickTree = createRawSnippet((vol: () => Volume) => ({
	render: () =>
		`<div style="padding:2px 6px 2px 18px;color:var(--rb-text-dim);font-style:italic">${vol().name} tree (UsbStickTree)</div>`
}));

const meta = {
	title: 'Components/Browser/UsbSourceListView',
	component: UsbSourceListView,
	tags: ['autodocs'],
	args: {
		volumes: [stick('vol:a1', 'DJ STICK'), stick('vol:b2', 'BACKUP USB')],
		openSticks: {},
		canImport: () => true,
		importTitle:
			'Open this stick and analyze BPM, key and beatgrid once its tracks are in the library.',
		importButtonLabel: 'Open',
		ontoggle: fn(),
		ondetails: fn(),
		onimport: fn(),
		onrowcontextmenu: fn(),
		onrowkeydown: fn(),
		stickTree
	},
	parameters: { layout: 'padded' }
} satisfies Meta<typeof UsbSourceListView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Two sticks, both closed and importable. */
export const TwoSticks: Story = {};

/** The first stick open: its tree draws under its row. */
export const StickOpen: Story = { args: { openSticks: { 'vol:a1': true } } };

/** macOS is still asking whether Open DJ may read removable volumes. */
export const AccessPending: Story = {
	args: { volumes: [stick('vol:a1', 'DJ STICK', { access: 'pending' })], canImport: () => false }
};

/** macOS refused access: a Settings deep link replaces the tree. */
export const AccessDenied: Story = {
	args: { volumes: [stick('vol:a1', 'DJ STICK', { access: 'denied' })], canImport: () => false }
};

/** No stick plugged in. */
export const Empty: Story = { args: { volumes: [] } };
