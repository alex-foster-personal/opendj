import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './SortArrowIcon.contract';
import SortArrowIcon from './SortArrowIcon';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Browser/SortArrowIcon', component: SortArrowIcon } satisfies Meta<typeof SortArrowIcon>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Ascending: Story = contracts.Ascending;
export const Descending: Story = contracts.Descending;
