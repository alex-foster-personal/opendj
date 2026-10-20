import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './DeckErrorBanner.contract';
import DeckErrorBanner from './DeckErrorBanner.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Deck/DeckErrorBanner', component: DeckErrorBanner } satisfies Meta<typeof DeckErrorBanner>;
export default meta;
type Story = StoryObj<typeof meta>;

export const WithId: Story = contracts.WithId;
export const NoIdYet: Story = contracts.NoIdYet;
export const LoadFailure: Story = contracts.LoadFailure;
