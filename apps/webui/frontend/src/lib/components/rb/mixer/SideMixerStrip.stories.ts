import type { Meta, StoryObj } from '@storybook/sveltekit';

import { deckStates, mixerState } from '$lib/player/state.svelte';
import type { DeckId } from '$lib/rb/deck-slots';
import type { StemDeckState } from '$lib/rb/stem-types';

import SideMixerStrip from './SideMixerStrip.svelte';
import { contracts } from './SideMixerStrip.contract';

/**
 * #9435 (epic #9371, MX-2): one deck's side mixer strip, both sides in three
 * skins. Plays live in SideMixerStrip.contract.ts for the React-twin / ratchet
 * delegation path.
 */

const DECKS: readonly DeckId[] = [1, 2, 3, 4];

const ctl = () => ({ muted: false, solo: false, gain: 0.5, fx_send: 0 });

function stems(): StemDeckState {
	return {
		status: 'ready',
		source: 'demucs',
		model: 'htdemucs',
		layout: 'demucs4',
		available_controls: ['vocal', 'instrumental', 'drums', 'bass', 'other'],
		alignment: null,
		controls: { vocal: ctl(), instrumental: ctl(), drums: ctl(), bass: ctl(), other: ctl() },
		error: null,
		load: null
	};
}

function resetState(): void {
	for (const id of DECKS) {
		const ch = mixerState.channels[id];
		ch.eq_high = 0.5;
		ch.eq_mid = 0.5;
		ch.eq_low = 0.5;
		ch.fader = 1;
	}
	deckStates[2].stems = stems();
}

const meta = {
	title: 'Contracts/Mixer/SideMixerStrip',
	component: SideMixerStrip,
	beforeEach: resetState
} satisfies Meta<typeof SideMixerStrip>;
export default meta;
type Story = StoryObj<typeof meta>;

export const OuterLeftDefault: Story = contracts.OuterLeftDefault;
export const OuterRightDefault: Story = contracts.OuterRightDefault;
export const OuterLeftGothic: Story = contracts.OuterLeftGothic;
export const OuterRightGothic: Story = contracts.OuterRightGothic;
export const OuterLeftLight: Story = contracts.OuterLeftLight;
export const OuterRightLight: Story = contracts.OuterRightLight;
