import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './AlphaBadge.contract';
import AlphaBadge from './AlphaBadge';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Root/AlphaBadge', component: AlphaBadge } satisfies Meta<typeof AlphaBadge>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = contracts.Default;
