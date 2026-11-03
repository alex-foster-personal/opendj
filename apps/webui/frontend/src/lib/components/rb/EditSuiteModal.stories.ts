import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';
import { expect, fn, within } from 'storybook/test';

import { contracts } from './EditSuiteModal.contract';
import { DEMO_BODY_TEXT } from './EditSuiteModal.contract';
import EditSuiteModal from './EditSuiteModal.svelte';

const body = (text: string) =>
	createRawSnippet(() => ({ render: () => `<p style="margin:0;padding:14px">${text}</p>` }));

/** Shared modal shell for find-replace, bulk-edit and mytag-editor. Escape or overlay click closes. */
const meta = {
	title: 'Contracts/Root/EditSuiteModal',
	component: EditSuiteModal,
	tags: ['autodocs'],
	args: {
		onclose: fn(),
		children: createRawSnippet(() => ({ render: () => `<p class="es-demo">${DEMO_BODY_TEXT}</p>` }))
	}
} satisfies Meta<typeof EditSuiteModal>;
export default meta;
type Story = StoryObj<typeof meta>;

export const ClickToClose: Story = contracts.ClickToClose;
export const EscapeToClose: Story = contracts.EscapeToClose;

export const Default: Story = {
	args: { title: 'Find and replace', children: body('Body content goes here.') },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByRole('dialog', { name: 'Find and replace' })).toBeInTheDocument();
	}
};

/** Body taller than the panel: the body region scrolls. */
export const LongBody: Story = {
	args: {
		title: 'Bulk edit 3 tracks',
		children: createRawSnippet(() => ({
			render: () =>
				`<div style="padding:14px">${Array.from({ length: 40 }, (_, i) => `<p>Row ${i + 1}: Night Drive (Extended Mix) - Lumen Fields</p>`).join('')}</div>`
		}))
	},
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByText('Row 40: Night Drive (Extended Mix) - Lumen Fields')).toBeInTheDocument();
	}
};

export const LongTitle: Story = {
	args: {
		title: 'Edit tags for Night Drive (Extended Mix) by Lumen Fields and 41 other selected tracks',
		children: body('Body content goes here.')
	},
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByRole('heading')).toHaveTextContent('Edit tags for Night Drive (Extended Mix) by Lumen Fields and 41 other selected tracks');
	}
};
