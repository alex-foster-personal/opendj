import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './SpinnerIcon.contract';
import SpinnerIcon from './SpinnerIcon.svelte';

const meta = {
	title: 'Contracts/Browser/SpinnerIcon',
	component: SpinnerIcon,
	tags: ['autodocs'],
	argTypes: { size: { control: { type: 'number', min: 8, max: 64 } } }
} satisfies Meta<typeof SpinnerIcon>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = contracts.Default;
export const Large: Story = contracts.Large;
