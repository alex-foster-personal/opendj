import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, fn, within } from 'storybook/test';

import type { PreflightExplainer } from '$lib/preflight/preflight-actions';

import { contracts } from './PreflightActions.contract';
import PreflightActions from './PreflightActions.svelte';

const explainers: PreflightExplainer[] = [
	{ label: 'Import your music', text: 'opens setup to import rekordbox or a music folder.' },
	{
		label: 'Re-check',
		text: 'runs every startup check again now instead of waiting for the next automatic check.'
	},
	{
		label: 'Re-request permissions',
		text: 'attempts the protected audio read again; macOS shows its prompt here if it never has.'
	}
];

const meta = {
	title: 'Contracts/Preflight/PreflightActions',
	component: PreflightActions,
	tags: ['autodocs'],
	args: {
		actions: ['import', 'recheck', 'permissions'],
		explainers,
		onAction: fn()
	}
} satisfies Meta<typeof PreflightActions>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Idle: Story = contracts.Idle;
export const ImportPending: Story = contracts.ImportPending;
export const RecheckPendingBlocked: Story = contracts.RecheckPendingBlocked;

/** The import action is running: all buttons disabled, import shows its busy label. */
export const PendingImport: Story = {
	args: { pending: 'import', status: 'Opening the importer...' },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTestId('preflight-action-import')).toHaveAttribute('aria-busy', 'true');
		await expect(within(canvasElement).getByTestId('preflight-action-recheck')).toBeDisabled();
	}
};

/** A non-import action is running. */
export const PendingRecheck: Story = {
	args: { pending: 'recheck', status: 'Re-checking...' },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTestId('preflight-action-recheck')).toHaveAttribute('aria-busy', 'true');
		await expect(within(canvasElement).getByTestId('preflight-action-import')).toBeDisabled();
	}
};

/** Import blocked: button disabled and the explainer says why. */
export const ImportDisabled: Story = {
	args: {
		disabled: ['import'],
		explainers: [
			{ label: 'Import your music', text: 'unavailable: the engine is not running yet.' },
			...explainers.slice(1)
		]
	},
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTestId('preflight-action-import')).toBeDisabled();
	}
};

/** Status line and footer note populated after a click. */
export const WithStatusAndFooter: Story = {
	args: {
		status: 'Re-checked at 21:04:12 - 1 check still failing.',
		footer: 'You can keep using the app with an empty library.'
	},
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByText('Re-checked at 21:04:12 - 1 check still failing.')).toBeInTheDocument();
		await expect(within(canvasElement).getByText('You can keep using the app with an empty library.')).toBeInTheDocument();
	}
};

/** The single retry action, as shown when the setup check itself failed. */
export const RetrySetupOnly: Story = {
	args: {
		actions: ['retry-setup'],
		explainers: [
			{
				label: 'Retry setup check',
				text: 'asks the engine again whether first-run setup is needed.'
			}
		]
	},
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTestId('preflight-action-retry-setup')).toBeEnabled();
	}
};
