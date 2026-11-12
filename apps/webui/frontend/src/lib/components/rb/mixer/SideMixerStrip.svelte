<script module lang="ts">
	/** Which outer edge of the deck row this strip sits on. */
	export type SideMixerSide = 'outer-left' | 'outer-right';
</script>

<script lang="ts">
	/**
	 * One deck's mixer controls as a side strip (epic #9371, MX-2): an EQ
	 * column (HI/MID/LOW), a STEM column (VOCAL/INST/BASS row for row, DRUMS
	 * as a 4th row) and a LEVEL column (meter + channel fader).
	 *
	 * Mirror rule (mimicked from Serato / Traktor horizontal layouts): the
	 * fader is always nearest the deck's outer edge, so `outer-left` reads
	 * LEVEL EQ STEM and `outer-right` STEM EQ LEVEL. The order is DOM order,
	 * so tab order and screen readers match what is drawn.
	 *
	 * Values are read from the shared mixerState and deck stores (the same
	 * state ChannelStrip shows, never a copy); writes go through the same
	 * dispatcher callbacks ChannelStrip takes. Mounting is MX-3, which renders
	 * either the centre strips or the side strips, never both (knob ids are
	 * per deck and role).
	 */
	import { mixerState } from '$lib/player/state.svelte';
	import { getDeckState } from '$lib/rb/audio-engine.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { EqBand } from '$lib/rb/mixer-types';
	import { knobId, type KnobRole } from '$lib/rb/knob-control.svelte';
	import type { StemControl } from '$lib/rb/stem-types';
	import { stemCssColor } from '$lib/rb/stem-colors';
	import { STEM_DIAL_LABELS, channelShowsStemDials, stemDialLayout } from '$lib/rb/stem-dial-map';
	import ChannelLevelMeter from './ChannelLevelMeter.svelte';
	import Knob from './Knob.svelte';
	import VFader from './VFader.svelte';

	interface Props {
		deckId: DeckId;
		side: SideMixerSide;
		/** The mixer's S button (uiPrefs.mixer_stems): stem dials when stems are ready. */
		mixerStems: boolean;
		oneq: (band: EqBand, value: number) => void;
		onfader: (value: number) => void;
		onStemGain: (stem: StemControl, value: number) => void;
	}

	let { deckId, side, mixerStems, oneq, onfader, onStemGain }: Props = $props();

	const COLUMNS = ['level', 'eq', 'stem'] as const;
	const columns = $derived.by(() => {
		if (side === 'outer-left') return COLUMNS;
		if (side === 'outer-right') return [...COLUMNS].reverse();
		throw new Error(`unknown side mixer side: ${String(side)}`);
	});

	/** Knob's default dial size, explicit for `exactOptionalPropertyTypes`. */
	const EQ_SIZE = 30;
	const channel = $derived(mixerState.channels[deckId]);
	const deck = $derived(getDeckState(deckId));
	const playing = $derived(deck.playing);
	const looped = $derived(deck.loop !== null && deck.loop.engaged);
	const stemDials = $derived(channelShowsStemDials(mixerStems, deck.stems));
	const stemLayout = $derived(stemDialLayout(deck.stems.available_controls));

	function stemKnobRole(stem: StemControl): KnobRole {
		if (stem === 'vocal') return 'stem-vocal';
		if (stem === 'instrumental') return 'stem-instrumental';
		if (stem === 'bass') return 'stem-bass';
		if (stem === 'drums') return 'stem-drums';
		throw new Error(`no mixer stem dial for ${stem}`);
	}
</script>

{#snippet stemDial(stem: StemControl)}
	<Knob
		knobId={knobId(deckId, stemKnobRole(stem))}
		label={STEM_DIAL_LABELS[stem]}
		accessibleLabel={`${STEM_DIAL_LABELS[stem].toLowerCase()} stem level deck ${deckId}`}
		value={deck.stems.controls[stem].gain}
		size={EQ_SIZE}
		accentColor={stemCssColor(stem)}
		onchange={(value) => {
			// Write the gain here, in the same turn as the pointer, so the
			// dial is not waiting on stem_gain to come back around the
			// command queue. The command still owns the audio graph.
			deck.stems.controls[stem].gain = value;
			onStemGain(stem, value);
		}}
	/>
{/snippet}

<div class="side-strip" data-testid={`side-mixer-strip-${deckId}`} data-side={side}>
	{#each columns as column (column)}
		{#if column === 'level'}
			<div class="col level" data-column="level">
				<ChannelLevelMeter {deckId} {playing} />
				<div class="fader-slot">
					<VFader
						value={channel.fader}
						{playing}
						{looped}
						{deckId}
						onchange={onfader}
						label={`channel fader deck ${deckId}`}
					/>
				</div>
			</div>
		{:else if column === 'eq'}
			<div class="col eq" data-column="eq">
				<Knob
					knobId={knobId(deckId, 'high')}
					label="HI"
					accessibleLabel={`high EQ deck ${deckId}`}
					value={channel.eq_high}
					size={EQ_SIZE}
					onchange={(value) => oneq('high', value)}
				/>
				<Knob
					knobId={knobId(deckId, 'mid')}
					label="MID"
					accessibleLabel={`mid EQ deck ${deckId}`}
					value={channel.eq_mid}
					size={EQ_SIZE}
					onchange={(value) => oneq('mid', value)}
				/>
				<Knob
					knobId={knobId(deckId, 'low')}
					label="LOW"
					accessibleLabel={`low EQ deck ${deckId}`}
					value={channel.eq_low}
					size={EQ_SIZE}
					onchange={(value) => oneq('low', value)}
				/>
			</div>
		{:else if column === 'stem'}
			<div class="col stem" data-column="stem">
				{#if stemDials}
					{#each stemLayout.column as stem, row (row)}
						{#if stem === null}
							<span class="stem-dial-empty" aria-hidden="true"></span>
						{:else}
							{@render stemDial(stem)}
						{/if}
					{/each}
					{#if stemLayout.filterPartner !== null}
						{@render stemDial(stemLayout.filterPartner)}
					{/if}
				{/if}
			</div>
		{/if}
	{/each}
</div>

<style>
	.side-strip {
		display: flex;
		gap: 4px;
		align-items: stretch;
		height: 100%;
		padding: 4px;
		box-sizing: border-box;
		background: var(--rb-panel, transparent);
	}
	.col {
		display: grid;
		grid-auto-rows: auto;
		align-content: start;
		justify-items: center;
		gap: 4px;
	}
	.col.level {
		display: flex;
		gap: 2px;
		align-items: stretch;
	}
	.fader-slot {
		display: flex;
		flex: 1 1 auto;
		min-height: 96px;
	}
	.stem-dial-empty {
		width: 30px;
		height: 30px;
	}
</style>
