import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './PadStrip.contract';
import PadStrip from './PadStrip.svelte';

/**
 * Decorative A-H letter strip. It is hidden in the shipped theme because
 * theme.css sets --rb-pad-strip-display: none on .perf-root.
 */
const meta = {
	title: 'Contracts/Deck/PadStrip',
	component: PadStrip,
	tags: ['autodocs'],
	parameters: { layout: 'padded' }
} satisfies Meta<typeof PadStrip>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = contracts.Default;
