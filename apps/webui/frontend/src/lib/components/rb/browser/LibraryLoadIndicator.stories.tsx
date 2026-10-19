import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './LibraryLoadIndicator.contract';
import LibraryLoadIndicator from './LibraryLoadIndicator';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Browser/LibraryLoadIndicator', component: LibraryLoadIndicator } satisfies Meta<typeof LibraryLoadIndicator>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Determinate: Story = contracts.Determinate;
export const LoadingMoreWhileSearching: Story = contracts.LoadingMoreWhileSearching;
export const SearchingOnly: Story = contracts.SearchingOnly;
export const Idle: Story = contracts.Idle;
