import type { Meta, StoryObj } from '@storybook/sveltekit';
import { http, HttpResponse } from 'msw';
import { expect, waitFor, within } from 'storybook/test';

import type { components } from '$lib/api-types';

import OpenInBrowserAction from './OpenInBrowserAction.svelte';

/**
 * Settings "Open in browser". The component reads GET /api/v1/ui-url. No
 * recording of that endpoint exists in src/sandbox/recorded/, so the body is
 * synthesized here and typed against the API schema (UiUrlOut), so schema
 * drift fails type-check. In Storybook there is no native shell and the page
 * host is local, so the action offers the address to copy.
 */
const UI_URL: components['schemas']['UiUrlOut'] = {
	ui_url: 'http://opendj.localhost:8765/',
	loopback_url: 'http://127.0.0.1:8765/',
	named_host: 'opendj.localhost',
	port: 8765,
	served_by: 'engine'
};
const uiUrlHandler = http.get('/api/v1/ui-url', () => HttpResponse.json(UI_URL));

const meta = {
	title: 'Contracts/Settings/OpenInBrowserAction',
	component: OpenInBrowserAction,
	tags: ['autodocs'],
	args: { compact: false },
	parameters: { msw: { handlers: [uiUrlHandler] } }
} satisfies Meta<typeof OpenInBrowserAction>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Full size: the address and a Copy button. */
export const Full: Story = {
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		const action = await canvas.findByTestId('open-in-browser');
		await waitFor(() => expect(action).toHaveAttribute('data-state', 'copy'));
		await expect(action.querySelector('.oib-url')).toHaveTextContent(UI_URL.ui_url);
		await expect(canvas.getByRole('button', { name: 'Copy address' })).toBeVisible();
		await expect(action).not.toHaveClass('compact');
	}
};

/** Compact: same content in the smaller row style. */
export const Compact: Story = {
	args: { compact: true },
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		const action = await canvas.findByTestId('open-in-browser');
		await waitFor(() => expect(action).toHaveAttribute('data-state', 'copy'));
		await expect(action.querySelector('.oib-url')).toHaveTextContent(UI_URL.ui_url);
		await expect(action).toHaveClass('compact');
	}
};

/** The daemon cannot report its address: the action is shown disabled. */
export const Unavailable: Story = {
	parameters: {
		msw: {
			handlers: [
				http.get('/api/v1/ui-url', () =>
					HttpResponse.json({ detail: { code: 'internal', message: 'ui url unavailable' } }, { status: 500 })
				)
			]
		}
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		const action = await canvas.findByTestId('open-in-browser');
		await waitFor(() => expect(action).toHaveAttribute('data-state', 'unavailable'));
		await expect(canvas.getByRole('button', { name: 'Open in browser' })).toBeDisabled();
	}
};
