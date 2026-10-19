import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './SpinnerIcon.contract';
import SpinnerIcon from './SpinnerIcon.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Browser/SpinnerIcon', component: SpinnerIcon } satisfies Meta<typeof SpinnerIcon>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = contracts.Default;
export const Large: Story = contracts.Large;
