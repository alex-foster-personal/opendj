import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './PadStrip.contract';
import PadStrip from './PadStrip.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Deck/PadStrip', component: PadStrip } satisfies Meta<typeof PadStrip>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = contracts.Default;
