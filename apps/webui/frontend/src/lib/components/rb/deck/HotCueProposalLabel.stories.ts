import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import { contracts } from './HotCueProposalLabel.contract';
import HotCueProposalLabel from './HotCueProposalLabel.svelte';

const meta = {
	title: 'Contracts/Deck/HotCueProposalLabel',
	component: HotCueProposalLabel,
	tags: ['autodocs'],
	argTypes: { kind: { control: 'text' } },
	args: { kind: 'drop' }
} satisfies Meta<typeof HotCueProposalLabel>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Drop: Story = contracts.Drop;
export const Unnamed: Story = contracts.Unnamed;
export const UnknownKind: Story = contracts.UnknownKind;

export const Intro: Story = {
	args: { kind: 'intro' },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.proposal-label')).toHaveTextContent('proposed intro');
	}
};

/** Empty kind falls back to the bare "proposed". */
export const NoKind: Story = {
	args: { kind: '' },
	play: async ({ canvasElement }) => {
		await expect(canvasElement.querySelector('.proposal-label')).toHaveTextContent(/^proposed$/);
	}
};
