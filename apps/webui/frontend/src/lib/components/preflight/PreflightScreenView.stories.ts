import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';
import { fn } from 'storybook/test';

import PreflightScreenView from './PreflightScreenView.svelte';

/** Stand-ins for the check rows, each a live PreflightCheckRow in the app. */
const rows = (...labels: string[]) =>
	createRawSnippet(() => ({
		render: () =>
			`<div>${labels.map((label) => `<li style="list-style:none;padding:2px 0">${label}</li>`).join('')}</div>`
	}));

const explainers = [
	{ label: 'Import your music', text: 'opens setup to import rekordbox or a music folder.' },
	{ label: 'Re-check', text: 'runs every startup check again now instead of waiting for the next automatic check.' },
	{
		label: 'Re-request permissions',
		text: 'attempts the protected audio read again; macOS shows its prompt here if it never has.'
	}
];

/**
 * The boot gate (PREFLIGHT-01) and its admin twin. The container polls the
 * checks and owns every action; the rows are placeholders here because each
 * is a live PreflightCheckRow. PreflightCheckRowView has its own stories.
 */
const meta = {
	title: 'Components/Preflight/PreflightScreenView',
	component: PreflightScreenView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: {
		mode: 'boot',
		blocking: true,
		showSurface: true,
		status: 'fail',
		bootHeadline: 'One thing needs your attention',
		engineError: null,
		firstRunHasError: false,
		firstRunError: null,
		visibleActions: ['import', 'recheck', 'permissions'],
		disabledActions: [],
		pendingAction: null,
		actionStatus: null,
		footer: 'Import your music below to continue, or fix the items above and re-check.',
		explainers,
		onAction: fn(),
		checkRows: rows('State database', 'Audio folder access', 'Library')
	}
} satisfies Meta<typeof PreflightScreenView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The blocking first-run screen with the brand header. */
export const BootBlocking: Story = {};

/** The engine could not be reached at all. */
export const EngineUnreachable: Story = {
	args: { engineError: 'TypeError: Failed to fetch', checkRows: rows() }
};

/** The first-run setup check itself failed: Retry setup check leads the actions. */
export const SetupCheckFailed: Story = {
	args: {
		firstRunHasError: true,
		firstRunError: 'Could not ask the engine whether setup is needed.',
		visibleActions: ['retry-setup', 'import', 'recheck', 'permissions']
	}
};

/** A re-check in flight. */
export const Rechecking: Story = { args: { pendingAction: 'recheck', actionStatus: 'Re-checking...' } };

/** The non-blocking strip shown once the app runs with advisories. */
export const BootStrip: Story = {
	args: { blocking: false, bootHeadline: 'Startup checks', footer: null, checkRows: rows('Audio output') }
};

/** The admin page's live reference. */
export const Admin: Story = {
	args: { mode: 'admin', bootHeadline: 'Preflight', footer: null, visibleActions: ['recheck', 'permissions'] }
};

/** Boot strip with nothing to show. */
export const Hidden: Story = { tags: ['renders-nothing'], args: { showSurface: false } };
