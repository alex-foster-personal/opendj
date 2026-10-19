import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './MissingTracksFolder.contract';
import MissingTracksFolder from './MissingTracksFolder';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = {
	title: 'Contracts/Browser/MissingTracksFolder',
	component: MissingTracksFolder
} satisfies Meta<typeof MissingTracksFolder>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Counted: Story = contracts.Counted;
export const LoadingSelected: Story = contracts.LoadingSelected;
export const Errored: Story = contracts.Errored;
