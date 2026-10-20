import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './SecondaryLoadBadge.contract';
import SecondaryLoadBadge from './SecondaryLoadBadge.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = {
	title: 'Contracts/Deck/SecondaryLoadBadge',
	component: SecondaryLoadBadge
} satisfies Meta<typeof SecondaryLoadBadge>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Loading: Story = contracts.Loading;
export const LoadingNoPhase: Story = contracts.LoadingNoPhase;
export const Failed: Story = contracts.Failed;
export const Settled: Story = contracts.Settled;
