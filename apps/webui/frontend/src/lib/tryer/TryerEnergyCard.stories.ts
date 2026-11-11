/**
 * Storybook contract for the try-er energy line and MIK strip (issue #10132).
 * Run: pnpm test:contracts src/lib/tryer/TryerEnergyCard.stories.ts
 */
import type { Meta, StoryObj } from '@storybook/sveltekit';

import TryerEnergyCard from './TryerEnergyCard.svelte';
import { contracts } from './TryerEnergyCard.contract';
import { TRYER_ENERGY_FIXTURE, tryerEnergyChip } from './energy-fixture';

const meta = {
	title: 'tryer/TryerEnergyCard',
	component: TryerEnergyCard,
	args: {
		chip: tryerEnergyChip(TRYER_ENERGY_FIXTURE),
		styleVisible: true
	}
} satisfies Meta<typeof TryerEnergyCard>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Scored: Story = {
	args: {
		chip: tryerEnergyChip(contracts.Scored.args.energy),
		styleVisible: true
	},
	play: contracts.Scored.play
};
