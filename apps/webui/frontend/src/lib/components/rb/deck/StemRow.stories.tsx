import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './StemRow.contract';
import StemRow from './StemRow';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Deck/StemRow', component: StemRow } satisfies Meta<typeof StemRow>;
export default meta;
type Story = StoryObj<typeof meta>;

export const ReadyDemucs: Story = contracts.ReadyDemucs;
export const RoformerChannel: Story = contracts.RoformerChannel;
export const FailedRetry: Story = contracts.FailedRetry;
export const Fetching: Story = contracts.Fetching;
export const NoStemsNoAction: Story = contracts.NoStemsNoAction;
export const NoStemsSeparate: Story = contracts.NoStemsSeparate;
export const NoStemsNoSource: Story = contracts.NoStemsNoSource;
export const NoStemsQueued: Story = contracts.NoStemsQueued;
export const NoStemsFailed: Story = contracts.NoStemsFailed;
export const QuickAca: Story = contracts.QuickAca;
export const QuickInst: Story = contracts.QuickInst;
export const QuickCustom: Story = contracts.QuickCustom;
export const QuickLoading: Story = contracts.QuickLoading;
