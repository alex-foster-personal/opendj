import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './PositionBar.contract';
import PositionBar from './PositionBar';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Root/PositionBar', component: PositionBar } satisfies Meta<typeof PositionBar>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Halfway: Story = contracts.Halfway;
export const UnknownDuration: Story = contracts.UnknownDuration;
export const PastEnd: Story = contracts.PastEnd;
