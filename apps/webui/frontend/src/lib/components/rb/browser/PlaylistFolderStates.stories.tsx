import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './PlaylistFolderStates.contract';
import PlaylistFolderStates from './PlaylistFolderStates';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Browser/PlaylistFolderStates', component: PlaylistFolderStates } satisfies Meta<typeof PlaylistFolderStates>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Loading: Story = contracts.Loading;
export const Error: Story = contracts.Error;
export const EmptyCreate: Story = contracts.EmptyCreate;
export const EmptyInert: Story = contracts.EmptyInert;
