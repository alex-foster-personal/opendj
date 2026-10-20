import type { Meta, StoryObj } from '@storybook/sveltekit';

import BrandLaunch from './BrandLaunch.svelte';
import { contracts } from './BrandLaunch.contract';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Root/BrandLaunch', component: BrandLaunch } satisfies Meta<typeof BrandLaunch>;
export default meta;
type Story = StoryObj<typeof meta>;

export const FirstLaunch: Story = contracts.FirstLaunch;
export const AlreadyPlayed: Story = contracts.AlreadyPlayed;
