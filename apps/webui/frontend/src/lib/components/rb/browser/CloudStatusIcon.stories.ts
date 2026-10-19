import type { Meta, StoryObj } from '@storybook/sveltekit';

import CloudStatusIcon from './CloudStatusIcon.svelte';
import { contracts } from './CloudStatusIcon.contract';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = {
	title: 'Contracts/Browser/CloudStatusIcon',
	component: CloudStatusIcon
} satisfies Meta<typeof CloudStatusIcon>;
export default meta;
type Story = StoryObj<typeof meta>;

export const NotOnCloud: Story = contracts.NotOnCloud;
export const OnCloudNotLocalGreenTick: Story = contracts.OnCloudNotLocalGreenTick;
export const StreamingSpotify: Story = contracts.StreamingSpotify;
export const StreamingSoundcloud: Story = contracts.StreamingSoundcloud;
