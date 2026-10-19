import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './RatingStars.contract';
import RatingStars from './RatingStars.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Browser/RatingStars', component: RatingStars } satisfies Meta<typeof RatingStars>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Rated: Story = contracts.Rated;
export const Unrated: Story = contracts.Unrated;
export const Disabled: Story = contracts.Disabled;
