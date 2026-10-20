import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './ScrubLyricStrip.contract';
import ScrubLyricStrip from './ScrubLyricStrip.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = {
	title: 'Contracts/Lyrics/ScrubLyricStrip',
	component: ScrubLyricStrip
} satisfies Meta<typeof ScrubLyricStrip>;
export default meta;
type Story = StoryObj<typeof meta>;

export const InsideWord: Story = contracts.InsideWord;
export const FarGap: Story = contracts.FarGap;
export const BeforeWordRadiusOne: Story = contracts.BeforeWordRadiusOne;
export const NothingToShow: Story = contracts.NothingToShow;
