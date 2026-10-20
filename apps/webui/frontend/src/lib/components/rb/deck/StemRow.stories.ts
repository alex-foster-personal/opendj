import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './StemRow.contract';
import StemRow from './StemRow.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Deck/StemRow', component: StemRow } satisfies Meta<typeof StemRow>;
export default meta;
type Story = StoryObj<typeof meta>;

export const ReadyDemucs: Story = contracts.ReadyDemucs;
export const RoformerChannel: Story = contracts.RoformerChannel;
export const FailedRetry: Story = contracts.FailedRetry;
export const Fetching: Story = contracts.Fetching;
