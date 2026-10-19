import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './FeedbackPinTopbarControls.contract';
import FeedbackPinTopbarControls from './FeedbackPinTopbarControls.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Root/FeedbackPinTopbarControls', component: FeedbackPinTopbarControls } satisfies Meta<typeof FeedbackPinTopbarControls>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Toggle: Story = contracts.Toggle;
export const ExternalSync: Story = contracts.ExternalSync;
