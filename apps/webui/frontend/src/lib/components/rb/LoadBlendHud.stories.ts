import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './LoadBlendHud.contract';
import LoadBlendHud from './LoadBlendHud.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Root/LoadBlendHud', component: LoadBlendHud } satisfies Meta<typeof LoadBlendHud>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Forward: Story = contracts.Forward;
export const Backward: Story = contracts.Backward;
export const Zero: Story = contracts.Zero;
