<script lang="ts">
	/**
	 * One mixer channel strip (SCREENSHOT-SPEC 4), top-down:
	 * channel number, TRIM, HI/MID/LOW, FILTER,
	 * headphone CUE, vertical fader (fills remaining height), STEM controls.
	 * Decks 3/4 render slightly lighter so 1/2 stay the visual focus.
	 */
	import { getDeckState } from '$lib/rb/audio-engine.svelte';
	import { deckHoverUi, setHoveredDeck } from '$lib/rb/deck-hover.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { EqBand } from '$lib/rb/mixer-types';
	import { knobId } from '$lib/rb/knob-control.svelte';
	import type { StemControl } from '$lib/rb/stem-types';
	import StemRow from '../deck/StemRow.svelte';
	import Knob from './Knob.svelte';
	import VFader from './VFader.svelte';

	interface Props {
		/** The deck this strip controls (screen order is 3 1 2 4). */
		deckId: DeckId;
		/** Pin 246b0f5: true while the MORE/LESS toggle (deck-layout-prefs.ts)
		 * is in LESS mode. LESS gives decks 1/2's strip far less height than
		 * MORE (the mixer shares `.deck-area`'s single grid row with the
		 * decks, and that row's floor shrinks in LESS - see +page.svelte's
		 * `.perf-root.deck-layout-less` comment), so the strip compacts:
		 * smaller TRIM/EQ knobs, tighter margins, and the FILTER dial drops
		 * out entirely. It is a live dial since issue #990, but it is the one
		 * control with a neutral resting value (0.5 = bypass) and a state
		 * that survives the strip not drawing it, so LESS still sheds it
		 * first. TRIM/EQ/CUE/the fader+level-meter/STEM stay. */
		less: boolean;
		/** TRIM knob 0..1; 0.5 = unity. */
		trim: number;
		/** HIGH knob 0..1; 0.5 = flat. */
		eqHigh: number;
		/** MID knob 0..1; 0.5 = flat. */
		eqMid: number;
		/** LOW knob 0..1; 0.5 = flat. */
		eqLow: number;
		/** FILTER knob 0..1; 0.5 = bypass. */
		filter: number;
		/** Channel fader 0..1; 1 = full. */
		fader: number;
		cueEnabled: boolean;
		stemPending: boolean;
		ontrim: (value: number) => void;
		oneq: (band: EqBand, value: number) => void;
		onfilter: (value: number) => void;
		onfader: (value: number) => void;
		oncue: (enabled: boolean) => void;
		onStemMute: (stem: StemControl) => Promise<void>;
		onStemSolo: (stem: StemControl) => Promise<void>;
	}

	let {
		deckId,
		less,
		trim,
		eqHigh,
		eqMid,
		eqLow,
		filter,
		fader,
		cueEnabled,
		stemPending,
		ontrim,
		oneq,
		onfilter,
		onfader,
		oncue,
		onStemMute,
		onStemSolo
	}: Props = $props();

	/** Decks 3/4 are secondary; lighten strip so 1/2 draw the eye. */
	const secondary = $derived(deckId === 3 || deckId === 4);
	const deck = $derived(getDeckState(deckId));
	const playing = $derived(deck.playing);
	const looped = $derived(deck.loop !== null && deck.loop.engaged);
	const focused = $derived(deckHoverUi.deckId === deckId);

	/** Halfway between TRIM's former 21px and the 30px EQ dials. MORE only -
	 * pin 246b0f5's LESS mode uses the smaller LESS_TRIM_SIZE below. */
	const TRIM_SIZE = 25.5;
	/** FILTER stays visually larger than the EQ stack, matching the mixer
	 * layout contract. The exact number is main's, not this branch's 39:
	 * channel-strip-less-floor.test.mjs derives the MORE floor from it. */
	const FILTER_SLOT_SIZE = 35.1;
	/** Pin 246b0f5 LESS mode: decks 1/2's strip has to fit inside the
	 * shrunk LESS deck-area row (see +page.svelte), so TRIM/EQ shrink and
	 * FILTER (inert stub, `{#if !less}` below) drops out -
	 * channel-strip-less-floor.test.mjs derives the LESS deck-area floor
	 * from these exact numbers, so a change here must stay in step with
	 * that test. */
	const LESS_TRIM_SIZE = 18;
	const LESS_EQ_SIZE = 18;
	/** Knob's own default dial size (see Knob.svelte's `size = 30`), spelled out
	 * explicitly here rather than omitted: `exactOptionalPropertyTypes` treats an
	 * explicit `size={undefined}` as distinct from the prop being absent, so
	 * `eqSize` must always resolve to a concrete number, same as `trimSize`. */
	const EQ_SIZE = 30;
	const trimSize = $derived(less ? LESS_TRIM_SIZE : TRIM_SIZE);
	const eqSize = $derived(less ? LESS_EQ_SIZE : EQ_SIZE);
</script>

<div
	class="strip"
	class:secondary
	class:less
	class:deck-focus={focused}
	data-mixer-channel={deckId}
	role="group"
	aria-label={`channel ${deckId}`}
	onpointerenter={() => setHoveredDeck(deckId)}
	onpointerleave={() => {
		if (deckHoverUi.deckId === deckId) setHoveredDeck(null);
	}}
>
	<span class="ch-num">{deckId}</span>
	<div class="trim-slot">
		<Knob
			knobId={knobId(deckId, 'trim')}
			label="TRIM"
			accessibleLabel={`trim deck ${deckId}`}
			value={trim}
			tone="white"
			size={trimSize}
			onchange={ontrim}
		/>
	</div>
	<div class="eq-stack">
		<Knob knobId={knobId(deckId, 'high')} label="HI" accessibleLabel={`high EQ deck ${deckId}`} value={eqHigh} size={eqSize} onchange={(v) => oneq('high', v)} />
		<Knob knobId={knobId(deckId, 'mid')} label="MID" accessibleLabel={`mid EQ deck ${deckId}`} value={eqMid} size={eqSize} onchange={(v) => oneq('mid', v)} />
		<Knob knobId={knobId(deckId, 'low')} label="LOW" accessibleLabel={`low EQ deck ${deckId}`} value={eqLow} size={eqSize} onchange={(v) => oneq('low', v)} />
	</div>
	{#if !less}
		<div class="filter-slot">
			<Knob
				knobId={knobId(deckId, 'filter')}
				label="FILTER"
				accessibleLabel={`filter deck ${deckId}`}
				value={filter}
				size={FILTER_SLOT_SIZE}
				onchange={onfilter}
			/>
		</div>
	{/if}
	<button
		class:enabled={cueEnabled}
		class="cue-btn"
		aria-pressed={cueEnabled}
		aria-label={`cue channel ${deckId}`}
		data-testid={`cue-channel-${deckId}`}
		onclick={() => oncue(!cueEnabled)}>CUE</button
	>
	<div class="fader-slot">
		<VFader
			value={fader}
			{playing}
			{looped}
			deckId={deckId}
			onchange={onfader}
			label={`channel fader deck ${deckId}`}
		/>
	</div>
	<span class="stem-label">STEM</span>
	<div class="stem-slot">
		<StemRow deck={deck} pending={stemPending} onMute={onStemMute} onSolo={onStemSolo} />
	</div>
</div>

<style>
	.strip {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: flex-start;
		gap: 2px;
		min-height: 0;
		height: 100%;
		/* Bottom pad opened up so STEM is not crowded against the strip's
		   lower border. The fader is `flex: 1 1 auto`, so the space comes out
		   of the channel level slider exactly as pin 8cd32a28c36d asks. */
		padding: 2px 2px 4px;
		border-radius: 2px;
	}
	.strip.secondary {
		background: color-mix(in srgb, var(--rb-panel-raised, #1a1e25) 55%, transparent);
	}
	.strip.deck-focus {
		transition:
			background 50ms ease-out,
			box-shadow 50ms ease-out;
		background: color-mix(in srgb, rgba(255, 255, 255, 0.1) 40%, var(--rb-panel-raised, #1a1e25));
		box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.22);
	}
	/* Every direct child except `.fader-slot` (which owns `flex: 1 1 auto`
	 * on purpose - it is the one element meant to absorb extra height) is
	 * fixed-size: explicit `flex-shrink: 0` so a too-short `.strip` overflows
	 * visibly (caught by `.rb-mixer`'s `overflow: hidden` and this file's
	 * e2e spec) instead of silently squeezing knobs/captions into an
	 * overlapping, sub-pixel mess that ships nothing to bite - the whole
	 * reason pin 246b0f5's fix sizes the deck-area LESS floor to real
	 * content instead of shrinking to fit whatever height happened to be
	 * left over. */
	.strip > :not(.fader-slot) {
		flex-shrink: 0;
	}
	.ch-num {
		font-size: 10px;
		color: var(--rb-text);
		line-height: 1;
	}
	/* Pin 246b0f5 FIX ROUND 3 (Sol P1/P2 BLOCKING, both on +page.svelte:281):
	 * this margin (and filter-slot's/cue-btn's/fader-slot's/stem-label's
	 * below) was tightened from its pre-fix value so the un-collapsed MORE
	 * strip's real content fits back inside the 497px deck-area floor
	 * LIBUX-01 documents as NOT reclaimable, instead of growing that floor
	 * to 524px (which broke both the short-window contract at 720px and
	 * the LIBUX-01 969-995px five-row guarantee - see channel-strip-less
	 * -floor.test.mjs's "MORE floor" test and +page.svelte's floor comment
	 * for the full arithmetic). Purely cosmetic spacing, no control removed
	 * or made smaller. */
	.trim-slot {
		margin-bottom: 3px;
	}
	/* Pin 246b0f5 LESS mode: FILTER (the inert stub below) drops out of the
	 * layout entirely, so the remaining vertical margins tighten further -
	 * channel-strip-less-floor.test.mjs derives the LESS deck-area floor
	 * from these exact numbers, so a change here must stay in step with
	 * that test. */
	.strip.less .trim-slot {
		margin-bottom: 3px;
	}
	.eq-stack {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 3px;
	}
	.filter-slot {
		margin-top: 1px;
		margin-bottom: 3px;
	}
	.cue-btn {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 8px;
		letter-spacing: 0.04em;
		padding: 2px 5px;
		line-height: 1;
		margin-top: 0;
		margin-bottom: 3px;
		flex: none;
		cursor: pointer;
	}
	.strip.less .cue-btn {
		margin-bottom: 4px;
	}
	.cue-btn.enabled {
		color: var(--rb-accent);
		border-color: var(--rb-accent);
	}
	.fader-slot {
		flex: 1 1 auto;
		min-height: 64px;
		width: 100%;
		display: flex;
		justify-content: center;
		align-items: stretch;
		margin-top: 0;
		margin-bottom: 4px;
	}
	.strip.less .fader-slot {
		margin-bottom: 4px;
	}
	.stem-label {
		font-size: 8px;
		letter-spacing: 0.06em;
		color: var(--rb-text-dim);
		line-height: 1;
		flex: none;
		/* 2px read as STEM touching the fader above it (pin 8cd32a28c36d). */
		margin-top: 2px;
	}
	.strip.less .stem-label {
		margin-top: 2px;
	}
	.stem-slot :global(.stems) {
		flex-direction: column;
		gap: 2px;
	}
	.stem-slot :global(.mute) {
		margin-right: 0;
	}
	.stem-slot :global(.chip) {
		font-size: 7px;
		padding: 1px 4px;
	}
</style>
