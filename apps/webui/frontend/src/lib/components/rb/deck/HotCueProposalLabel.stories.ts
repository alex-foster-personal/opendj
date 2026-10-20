import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './HotCueProposalLabel.contract';
import HotCueProposalLabel from './HotCueProposalLabel.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Deck/HotCueProposalLabel', component: HotCueProposalLabel } satisfies Meta<typeof HotCueProposalLabel>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Drop: Story = contracts.Drop;
export const Unnamed: Story = contracts.Unnamed;
export const UnknownKind: Story = contracts.UnknownKind;
