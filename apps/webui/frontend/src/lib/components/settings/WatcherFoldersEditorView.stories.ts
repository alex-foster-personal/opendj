import type { Meta, StoryObj } from '@storybook/sveltekit';
import type { ComponentProps } from 'svelte';
import { fn } from 'storybook/test';

import WatcherFoldersEditorView from './WatcherFoldersEditorView.svelte';

type SettingDef = ComponentProps<typeof WatcherFoldersEditorView>['def'];

/** The catalog row this editor draws (LIBM-129), copied in shape, not imported. */
const def: SettingDef = {
	id: 'library_watcher_folders',
	label: 'Watcher folders',
	group: 'library',
	keywords: ['watcher', 'folder'],
	title: 'Folder paths to watch for auto-import (v2 - not active yet)',
	detail: 'One absolute path per line, checked to exist on save. No watcher runs until v2.',
	implemented: true,
	control: {
		kind: 'path_lines',
		notice: 'v2 - folder watcher not active; paths are stored for a future release only.'
	}
};

/**
 * The settings overlay's watcher-folders row. The draft is bindable; Apply
 * raises `onApply` (Actions panel), and the container is what validates and
 * saves. Paths are invented.
 */
const meta = {
	title: 'Components/Settings/WatcherFoldersEditorView',
	component: WatcherFoldersEditorView,
	tags: ['autodocs'],
	args: { def, draft: '', busy: false, msg: null, onApply: fn() }
} satisfies Meta<typeof WatcherFoldersEditorView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Nothing stored yet. */
export const Empty: Story = {};

/** Two saved folders, with the confirmation from the last Apply. */
export const Saved: Story = {
	args: { draft: '/music/Incoming\n/Volumes/Crates/Promos', msg: 'Saved 2 folder(s).' }
};

/** Apply in flight: the button is disabled and reads Saving. */
export const Saving: Story = {
	args: { draft: '/music/Incoming', busy: true }
};

/** The existence check refused a path. */
export const Refused: Story = {
	args: { draft: '/music/Missing', msg: 'Folder does not exist: /music/Missing' }
};
