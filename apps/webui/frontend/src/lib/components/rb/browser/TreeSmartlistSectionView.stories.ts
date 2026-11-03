import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { ComponentProps } from 'svelte';

import TreeSmartlistSectionView from './TreeSmartlistSectionView.svelte';

/**
 * The tree's Smartlists folder: a disclosure row, then one row per smartlist
 * with its rule summary as the hover title and its live count. The container
 * owns the rows, the inline rename and the context menu. Names and rules are
 * invented.
 */
type Smartlist = NonNullable<ComponentProps<typeof TreeSmartlistSectionView>['rows']>[number];

const smartlist = (id: string, name: string, rule_summary: string, count: number | null): Smartlist => ({
	id,
	name,
	rule: { field: 'bpm', op: 'between', value: [120, 126] },
	rule_summary,
	order_by: 'title',
	referenced_fields: ['bpm'],
	rule_schema_version: 1,
	last_evaluated_at: '2026-10-12T20:00:00Z',
	created_at: '2026-10-01T20:00:00Z',
	modified_at: '2026-10-12T20:00:00Z',
	count
});

const rows = [
	smartlist('sl-1', 'House 120-126', 'BPM between 120 and 126', 842),
	smartlist('sl-2', 'Unplayed This Month', 'Last played before 30 days ago', 1530),
	smartlist('sl-3', 'Broken Rule', 'Genre is Techno', null)
];

const meta = {
	title: 'Components/Browser/TreeSmartlistSectionView',
	component: TreeSmartlistSectionView,
	tags: ['autodocs'],
	args: {
		open: true,
		rows,
		error: null,
		selectedId: null,
		selectable: true,
		editingId: null,
		editDraft: '',
		ontoggle: fn(),
		onclickrow: fn(),
		onmenukey: fn(),
		oncontextmenurow: fn(),
		oneditdraft: fn(),
		onrenamekeydown: fn(),
		onrenamecommit: fn()
	},
	parameters: { layout: 'padded' }
} satisfies Meta<typeof TreeSmartlistSectionView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Open with three smartlists; a failed evaluation shows '--'. */
export const Typical: Story = {};

/** One smartlist is open in the active pane. */
export const Selected: Story = { args: { selectedId: 'sl-1' } };

/** Inline rename in progress on the first row. */
export const Renaming: Story = { args: { editingId: 'sl-1', editDraft: 'House 120-128' } };

/** No smartlist selection wired: every row renders inert. */
export const Inert: Story = { args: { selectable: false } };

/** Folder collapsed. */
export const Collapsed: Story = { args: { open: false } };

/** Rows still loading. */
export const Loading: Story = { args: { rows: null } };

/** No smartlists defined. */
export const Empty: Story = { args: { rows: [] } };

/** The list failed; the reason is in the hover title. */
export const Error: Story = { args: { rows: null, error: 'smartlists_unavailable' } };
