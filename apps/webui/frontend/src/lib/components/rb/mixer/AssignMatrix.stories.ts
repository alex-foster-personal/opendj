import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './AssignMatrix.contract';
import AssignMatrix from './AssignMatrix.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Mixer/AssignMatrix', component: AssignMatrix } satisfies Meta<typeof AssignMatrix>;
export default meta;
type Story = StoryObj<typeof meta>;

export const BusA: Story = contracts.BusA;
export const BusB: Story = contracts.BusB;
