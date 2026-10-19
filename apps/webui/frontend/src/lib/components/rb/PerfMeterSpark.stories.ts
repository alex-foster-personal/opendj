import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './PerfMeterSpark.contract';
import PerfMeterSpark from './PerfMeterSpark.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Root/PerfMeterSpark', component: PerfMeterSpark } satisfies Meta<typeof PerfMeterSpark>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Labeled: Story = contracts.Labeled;
export const Bare: Story = contracts.Bare;
export const EmptyTitle: Story = contracts.EmptyTitle;
