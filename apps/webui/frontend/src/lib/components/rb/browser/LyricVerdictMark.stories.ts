import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './LyricVerdictMark.contract';
import LyricVerdictMark from './LyricVerdictMark.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Browser/LyricVerdictMark', component: LyricVerdictMark } satisfies Meta<typeof LyricVerdictMark>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Vocal: Story = contracts.Vocal;
export const Pending: Story = contracts.Pending;
export const Sparse: Story = contracts.Sparse;
