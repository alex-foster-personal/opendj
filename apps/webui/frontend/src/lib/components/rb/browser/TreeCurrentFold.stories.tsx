import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './TreeCurrentFold.contract';
import TreeCurrentFold from './TreeCurrentFold';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Browser/TreeCurrentFold', component: TreeCurrentFold } satisfies Meta<typeof TreeCurrentFold>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Above: Story = contracts.Above;
export const Below: Story = contracts.Below;
export const InView: Story = contracts.InView;
