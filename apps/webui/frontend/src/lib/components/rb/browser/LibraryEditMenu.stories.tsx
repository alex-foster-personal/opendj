import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './LibraryEditMenu.contract';
import LibraryEditMenu from './LibraryEditMenu';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = {
	title: 'Contracts/Browser/LibraryEditMenu',
	component: LibraryEditMenu
} satisfies Meta<typeof LibraryEditMenu>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Closed: Story = contracts.Closed;
export const WithSelection: Story = contracts.WithSelection;
export const NoSelection: Story = contracts.NoSelection;
export const Dismiss: Story = contracts.Dismiss;
