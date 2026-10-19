import type { Meta, StoryObj } from '@storybook/sveltekit';

import BrowserConfirmDialog from './BrowserConfirmDialog.svelte';
import { contracts } from './BrowserConfirmDialog.contract';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = {
	title: 'Contracts/Browser/BrowserConfirmDialog',
	component: BrowserConfirmDialog
} satisfies Meta<typeof BrowserConfirmDialog>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Open: Story = contracts.Open;
export const Defaults: Story = contracts.Defaults;
export const DefaultOnlySecondary: Story = contracts.DefaultOnlySecondary;
export const BothBoxes: Story = contracts.BothBoxes;
export const Closed: Story = contracts.Closed;
