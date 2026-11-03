import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';

import CrateDeskView from './CrateDeskView.svelte';

/** A labeled placeholder in one grid area, standing in for a live region. */
const region = (area: string, label: string) =>
	createRawSnippet(() => ({
		render: () =>
			`<div style="grid-area:${area};display:flex;align-items:center;justify-content:center;border:1px dashed var(--rb-border);color:var(--rb-text-dim);font-size:12px">${label}</div>`
	}));

/**
 * The Library workspace (LIBWS-01) grid: top bar, preview bar, then the
 * browser beside a 300px inspector. Every region is a live container in the
 * app, so CrateDesk passes them in as snippets; here each is a labeled
 * placeholder so the layout itself is what the story shows.
 */
const meta = {
	title: 'Components/Library/CrateDeskView',
	component: CrateDeskView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: {
		topbar: region('topbar', 'LibraryTopBar'),
		preview: region('preview', 'LibraryPreviewBar'),
		browser: region('browser', 'BrowserPanel'),
		inspector: region('inspector', 'TrackInspector')
	}
} satisfies Meta<typeof CrateDeskView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The four regions in their grid areas. */
export const Layout: Story = {};
