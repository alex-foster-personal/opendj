import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './ContextMenu.contract';
import ContextMenu from './ContextMenu';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Root/ContextMenu', component: ContextMenu } satisfies Meta<typeof ContextMenu>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Items: Story = contracts.Items;
export const Activate: Story = contracts.Activate;
export const Keyboard: Story = contracts.Keyboard;
export const Clamped: Story = contracts.Clamped;
