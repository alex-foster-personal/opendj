import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './BannerWarning.contract';
import BannerWarning from './BannerWarning';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Root/BannerWarning', component: BannerWarning } satisfies Meta<typeof BannerWarning>;
export default meta;
type Story = StoryObj<typeof meta>;

export const ReadOnly: Story = contracts.ReadOnly;
export const DaemonDown: Story = contracts.DaemonDown;
