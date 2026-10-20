import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './PadModeMenu.contract';
import PadModeMenu from './PadModeMenu';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Deck/PadModeMenu', component: PadModeMenu } satisfies Meta<typeof PadModeMenu>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Closed: Story = contracts.Closed;
export const Open: Story = contracts.Open;
export const Dismiss: Story = contracts.Dismiss;
export const Stacked: Story = contracts.Stacked;
