import type { Meta, StoryObj } from '@storybook/react-vite';
import { useState } from 'react';

import BrowserConfirmDialog, { type BrowserConfirmDialogProps } from './BrowserConfirmDialog';
import { contracts } from './BrowserConfirmDialog.contract';

/** The twin is controlled; this harness is the parent that holds `open`,
 * standing where bind:open stands for the Svelte story. */
function ControlledBrowserConfirmDialog(props: Omit<BrowserConfirmDialogProps, 'onOpenChange'>) {
	const [open, setOpen] = useState(props.open);
	return <BrowserConfirmDialog {...props} open={open} onOpenChange={setOpen} />;
}

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = {
	title: 'Contracts/Browser/BrowserConfirmDialog',
	component: ControlledBrowserConfirmDialog
} satisfies Meta<typeof ControlledBrowserConfirmDialog>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Open: Story = contracts.Open;
export const Defaults: Story = contracts.Defaults;
export const DefaultOnlySecondary: Story = contracts.DefaultOnlySecondary;
export const BothBoxes: Story = contracts.BothBoxes;
export const Closed: Story = contracts.Closed;
