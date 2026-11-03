import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './SortArrowIcon.contract';
import SortArrowIcon from './SortArrowIcon.svelte';

const meta = {
	title: 'Contracts/Browser/SortArrowIcon',
	component: SortArrowIcon,
	tags: ['autodocs']
} satisfies Meta<typeof SortArrowIcon>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Ascending: Story = contracts.Ascending;
export const Descending: Story = contracts.Descending;
