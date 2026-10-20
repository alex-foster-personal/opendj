import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './FaderRejoinGuidance.contract';
import FaderRejoinGuidance from './FaderRejoinGuidance';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Mixer/FaderRejoinGuidance', component: FaderRejoinGuidance } satisfies Meta<typeof FaderRejoinGuidance>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Short: Story = contracts.Short;
export const Long: Story = contracts.Long;
