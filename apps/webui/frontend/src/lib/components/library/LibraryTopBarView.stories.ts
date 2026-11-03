import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';
import { fn } from 'storybook/test';

import { APP_MODES, appModeForPath } from '$lib/rb/app-mode';
import { SIDEBAR_NAV_LINKS } from '$lib/shell/sidebar-nav';

import LibraryTopBarView from './LibraryTopBarView.svelte';

/** Stands in for the CloudSync chip, a live container in the app. */
const chip = createRawSnippet(() => ({
	render: () =>
		'<span style="border:1px dashed var(--rb-border);border-radius:999px;padding:1px 8px">sync</span>'
}));

/**
 * The Library workspace top bar (LIBWS-01): mode picker, the shell's page
 * links, the inert quick selector, library counts, the selection count and
 * the CloudSync chip (a placeholder here). Modes and links are the app's own
 * tables; counts are invented.
 */
const meta = {
	title: 'Components/Library/LibraryTopBarView',
	component: LibraryTopBarView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: {
		activeMode: appModeForPath('/'),
		pageLinks: SIDEBAR_NAV_LINKS.filter((link) => !link.dev),
		pathname: '/',
		selectedCount: 0,
		libraryCounts: { tracks: 8355, playlists: 214 },
		selectMode: fn(),
		modeForHref: (href: string) => APP_MODES.find((mode) => mode.href === href),
		cloudSyncChip: chip
	}
} satisfies Meta<typeof LibraryTopBarView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Library mode, nothing selected. */
export const Default: Story = {};

/** Several tracks selected. */
export const WithSelection: Story = { args: { selectedCount: 42 } };

/** Health has not answered yet: no library counts. */
export const CountsPending: Story = { args: { libraryCounts: null } };

/** Every mode the picker offers, with the first one active. */
export const OtherMode: Story = { args: { activeMode: APP_MODES[0] } };
