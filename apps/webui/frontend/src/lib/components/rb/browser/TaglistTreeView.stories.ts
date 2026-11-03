import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import TaglistTreeView from './TaglistTreeView.svelte';

/**
 * The Taglists tab body: one row per MyTag with its track count. The
 * container loads the catalog and keeps it live on mytags events; this draws
 * it. Tag names are invented.
 */
const meta = {
	title: 'Components/Browser/TaglistTreeView',
	component: TaglistTreeView,
	tags: ['autodocs'],
	args: { catalog: null, error: null, selectedId: null, onselect: fn() },
	parameters: { layout: 'padded' }
} satisfies Meta<typeof TaglistTreeView>;

export default meta;
type Story = StoryObj<typeof meta>;

const tags = [
	{ name: 'Warmup', track_count: 48 },
	{ name: 'Vocal', track_count: 213 },
	{ name: 'Closing', track_count: 17 },
	{ name: 'Big Room Peak With A Very Long Tag Name', track_count: 1204 }
];

export const Typical: Story = {
	args: { catalog: { catalog_revision: 'r1', tags } }
};

/** The 'Vocal' taglist is open in the active pane. */
export const Selected: Story = {
	args: { catalog: { catalog_revision: 'r1', tags }, selectedId: 'taglist:Vocal' }
};

/** Catalog not loaded yet. */
export const Loading: Story = {};

/** No MyTags defined. */
export const Empty: Story = { args: { catalog: { catalog_revision: 'r1', tags: [] } } };

/** The catalog read failed; the error code is in the hover title. */
export const Error: Story = { args: { error: 'mytags_unavailable' } };
