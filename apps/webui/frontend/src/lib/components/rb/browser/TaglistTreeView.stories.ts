import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn } from 'storybook/test';

import { contracts } from './TaglistTreeView.contract';
import TaglistTreeView from './TaglistTreeView.svelte';

/**
 * The Taglists tab body: one row per MyTag with its track count. The
 * container loads the catalog and keeps it live on mytags events; this draws
 * it. Tag names are invented.
 */
const meta = {
	title: 'Contracts/Browser/TaglistTreeView',
	component: TaglistTreeView,
	tags: ['autodocs'],
	args: { catalog: null, error: null, selectedId: null, onselect: fn() },
	parameters: { layout: 'padded' }
} satisfies Meta<typeof TaglistTreeView>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Loading: Story = contracts.Loading;
export const ErrorNotEmpty: Story = contracts.ErrorNotEmpty;
export const EmptyLoaded: Story = contracts.EmptyLoaded;
export const RecoveredAfterError: Story = contracts.RecoveredAfterError;

const tags = [
	{ name: 'Warmup', track_count: 48 },
	{ name: 'Vocal', track_count: 213 },
	{ name: 'Closing', track_count: 17 },
	{ name: 'Big Room Peak With A Very Long Tag Name', track_count: 1204 }
];

export const Typical: Story = {
	args: { catalog: { catalog_revision: 'r1', tags } },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelectorAll('[data-testid="taglist-row"]')).toHaveLength(4);
		await expect(canvasElement.querySelector('[data-testid="taglists-error"]')).toBeNull();
		await expect(canvasElement.querySelector('[data-testid="taglists-empty"]')).toBeNull();
	}
};

/** The 'Vocal' taglist is open in the active pane. */
export const Selected: Story = {
	args: { catalog: { catalog_revision: 'r1', tags }, selectedId: 'taglist:Vocal' },
	play: async ({ canvasElement }) => {
		const vocal = canvasElement
			.querySelector('[data-testid="taglist-row"] .name[title="Vocal"]')
			?.closest('[data-testid="taglist-row"]');
		await expect(vocal).toHaveClass('selected');
	}
};

/** No MyTags defined. */
export const Empty: Story = {
	args: { catalog: { catalog_revision: 'r1', tags: [] } },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="taglists-empty"]')).toHaveTextContent(
			'No tags yet'
		);
		await expect(canvasElement.querySelector('[data-testid="taglist-row"]')).toBeNull();
	}
};

/** The catalog read failed; the error code is in the hover title. */
export const Error: Story = {
	args: { error: 'mytags_unavailable' },
	play: async ({ canvasElement }) => {
		const row = canvasElement.querySelector('[data-testid="taglists-error"]');
		await expect(row).toHaveTextContent('taglists unavailable');
		await expect(row).toHaveAttribute('title', 'mytags_unavailable');
		await expect(canvasElement.querySelector('[data-testid="taglists-empty"]')).toBeNull();
	}
};
