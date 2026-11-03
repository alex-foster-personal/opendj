import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, within } from 'storybook/test';

import { contracts } from './MinorIssueSquare.contract';
import MinorIssueSquare from './MinorIssueSquare.svelte';

const meta = {
	title: 'Contracts/Browser/MinorIssueSquare',
	component: MinorIssueSquare,
	tags: ['autodocs']
} satisfies Meta<typeof MinorIssueSquare>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Several: Story = contracts.Several;
export const One: Story = contracts.One;
export const None: Story = contracts.None;

/** One problem: the square shows, the hover title names it. */
export const SingleIssue: Story = {
	args: { issues: [{ id: 'missing-genre', label: 'Missing genre' }] },
	play: async ({ canvasElement }) => {
		await expect(within(canvasElement).getByTestId('minor-issue-square')).toHaveAttribute('aria-label', 'Missing genre');
	}
};

/** Several problems collapse into one square; the title joins every label. */
export const MultipleIssues: Story = {
	args: {
		issues: [
			{ id: 'not-on-cloud', label: 'Not on CloudSync' },
			{ id: 'low-quality', label: 'Low quality audio' },
			{ id: 'no-lyrics', label: 'No lyrics' }
		]
	},
	play: async ({ canvasElement }) => {
		const label = within(canvasElement).getByTestId('minor-issue-square').getAttribute('aria-label') ?? '';
		await expect(label).toContain('Not on CloudSync');
		await expect(label).toContain('Low quality audio');
		await expect(label).toContain('No lyrics');
	}
};

/** No issues: the component renders nothing at all. */
export const Empty: Story = {
	tags: ['renders-nothing'],
	args: { issues: [] },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('[data-testid="minor-issue-square"]')).toBeNull();
	}
};
