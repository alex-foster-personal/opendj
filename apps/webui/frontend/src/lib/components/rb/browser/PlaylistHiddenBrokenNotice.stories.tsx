import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './PlaylistHiddenBrokenNotice.contract';
import PlaylistHiddenBrokenNotice from './PlaylistHiddenBrokenNotice';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Browser/PlaylistHiddenBrokenNotice', component: PlaylistHiddenBrokenNotice } satisfies Meta<typeof PlaylistHiddenBrokenNotice>;
export default meta;
type Story = StoryObj<typeof meta>;

export const One: Story = contracts.One;
export const Several: Story = contracts.Several;
export const None: Story = contracts.None;
