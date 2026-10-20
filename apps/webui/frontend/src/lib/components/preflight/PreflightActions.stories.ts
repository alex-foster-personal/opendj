import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './PreflightActions.contract';
import PreflightActions from './PreflightActions.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = {
	title: 'Contracts/Preflight/PreflightActions',
	component: PreflightActions
} satisfies Meta<typeof PreflightActions>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Idle: Story = contracts.Idle;
export const ImportPending: Story = contracts.ImportPending;
export const RecheckPendingBlocked: Story = contracts.RecheckPendingBlocked;
