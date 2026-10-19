import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './AddToPlaylistPicker.contract';
import AddToPlaylistPicker from './AddToPlaylistPicker.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Browser/AddToPlaylistPicker', component: AddToPlaylistPicker } satisfies Meta<typeof AddToPlaylistPicker>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Few: Story = contracts.Few;
export const ManyWithFilter: Story = contracts.ManyWithFilter;
export const Empty: Story = contracts.Empty;
export const Closed: Story = contracts.Closed;
