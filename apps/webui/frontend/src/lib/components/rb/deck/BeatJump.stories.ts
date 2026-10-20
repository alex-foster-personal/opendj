import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './BeatJump.contract';
import BeatJump from './BeatJump.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Deck/BeatJump', component: BeatJump } satisfies Meta<typeof BeatJump>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Ready: Story = contracts.Ready;
export const AtFirstBeat: Story = contracts.AtFirstBeat;
export const Pending: Story = contracts.Pending;
export const NoBeatgrid: Story = contracts.NoBeatgrid;
