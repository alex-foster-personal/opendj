<script lang="ts">
	/**
	 * One mixer channel strip (SCREENSHOT-SPEC 4), top-down:
	 * channel number, TRIM, HI/MID/LOW, FILTER,
	 * headphone CUE, vertical fader (fills remaining height), STEM controls.
	 * With the mixer's S button on and stems ready, VOCAL/INST/BASS stand in
	 * a column beside HI/MID/LOW and DRUMS beside FILTER (stem-dial-map.ts).
	 * Decks 3/4 render slightly lighter so 1/2 stay the visual focus.
	 */
	import AutoGainReadout from './AutoGainReadout.svelte';
	import { getDeckState, peekDeckMeterReading } from '$lib/rb/audio-engine.svelte';
	import { deckHoverLinkId, deckHoverUi, setHoveredDeck } from '$lib/rb/deck-hover.svelte';
	import { clickSelect, isSelected } from '$lib/rb/mixer-selection.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';
	import { METER_FLOOR_DBFS, calibrationCaptureDb } from '$lib/rb/meter-math';
	import type { EqBand } from '$lib/rb/mixer-types';
	import { knobId, stemKnobRole, type KnobRole } from '$lib/rb/knob-control.svelte';
	import {
		setLevelCalibrationCapture,
		setLevelCalibrationDisabled,
		uiPrefs
	} from '$lib/rb/prefs.svelte';
	import type { StemControl } from '$lib/rb/stem-types';
	import { stemCssColor } from '$lib/rb/stem-colors';
	import { STEM_DIAL_LABELS, channelShowsStemDials, stemDialLayout } from '$lib/rb/stem-dial-map';
	import { COLOR_FX_EFFECTS, COLOR_FX_LABELS, COLOR_FX_NEXT, isColorFxCycleGesture, type ColorFxChoice, type ColorFxEchoBeats, type ColorFxEffect } from '$lib/rb/color-fx';
	import { COLOR_FX_DIVISIONS, COLOR_FX_DIVISION_LABELS, echoDivisionTitle, showsEchoDivisions } from '$lib/rb/color-fx-division';
	import { colorFx } from '$lib/rb/color-fx.svelte';
	import {
		COLOR_FX_MENU_CLOSED,
		classifyColorFxPress,
		colorFxMenuClose,
		colorFxMenuOnPointerDown,
		colorFxMenuOnTap
	} from '$lib/rb/color-fx-menu';
	import { dialClockLabel } from '$lib/rb/dial-clock';
	import { quickPickModifier } from '$lib/rb/cmdk-actions';
	import StemRow from '../deck/StemRow.svelte';
	import { toggleTrackLoadResetPanel, trackLoadResetEnabled } from '$lib/rb/track-load-reset.svelte';
	import Knob from './Knob.svelte';
	import VFader from './VFader.svelte';
	import { selId, selectorNode } from '$lib/keyboard/selector-register';

	interface Props {
		/** The deck this strip controls (screen order is 3 1 2 4). */
		deckId: DeckId;
		/** Pin 246b0f5: true while the MORE/LESS toggle (deck-layout-prefs.ts)
		 * is in LESS mode. LESS gives decks 1/2's strip far less height than
		 * MORE (the mixer shares `.deck-area`'s single grid row with the
		 * decks, and that row's floor shrinks in LESS - see +page.svelte's
		 * `.perf-root.deck-layout-less` comment), so the strip compacts:
		 * smaller TRIM/EQ/FILTER knobs, tighter margins, and a three-column
		 * grid that puts the fader left of the EQs and STEM right of them.
		 * LESS used to SHED the FILTER dial instead of shrinking it, on the
		 * grounds that its resting value is neutral (0.5 = bypass) and its
		 * state survives not being drawn. Pin 2917b0eca218 reversed that: an
		 * unmounted dial is a dial the DJ cannot reach mid-mix, and the grid
		 * makes room for every control. Nothing is unmounted by LESS now. */
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
		/** The mixer's S button (uiPrefs.mixer_stems): stem dials on every channel with stems. */
		mixerStems: boolean;
		stemPending: boolean;
		ontrim: (value: number) => void;
		/** AUTOGAIN-04: set (dB) or clear (null) the user's auto gain via the dispatcher. */
		onAutoGainOffset: (offsetDb: number | null, stableId: string) => Promise<{ ok: boolean; error?: unknown }>;
		oneq: (band: EqBand, value: number) => void;
		onfilter: (value: number) => void;
		/** COLORFX-02: pick what the color knob plays on this channel; COLORFX-8341: `next` cycles. */
		oncolorfx: (effect: ColorFxChoice) => void;
		/** COLORFX-10320: the ECHO division picked in the color FX menu (dispatches color_fx echo_beats). */
		onechodivision: (beats: ColorFxEchoBeats) => void;
		onfader: (value: number) => void;
		oncue: (enabled: boolean) => void;
		onStemGain: (stem: StemControl, value: number) => void;
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
		mixerStems,
		stemPending,
		ontrim,
		onAutoGainOffset,
		oneq,
		onfilter,
		oncolorfx,
		onechodivision,
		onfader,
		oncue,
		onStemGain,
		onStemMute,
		onStemSolo
	}: Props = $props();

	/** Decks 3/4 are secondary; lighten strip so 1/2 draw the eye. */
	const secondary = $derived(deckId === 3 || deckId === 4);
	/** #9397: LESS collapses the deck 3/4 strip columns, so they leave the keyboard selector. */
	const channelSelectable = $derived(!(less && secondary));
	const deck = $derived(getDeckState(deckId));
	const playing = $derived(deck.playing);
	const looped = $derived(deck.loop !== null && deck.loop.engaged);
	const focused = $derived(deckHoverLinkId(uiPrefs.deck_hover_link) === deckId);
	const selected = $derived(isSelected(deckId));

	// ------------------------------------------------------- color FX (COLORFX-02)

	/** Light tints for the non-FILTER effects (MIXUX-02: "lightly color coded"). */
	const COLOR_FX_TINT: Readonly<Record<(typeof COLOR_FX_EFFECTS)[number], string | undefined>> = {
		filter: undefined,
		space: '#a99cff',
		echo: '#6fd6c6'
	};
	const colorSettings = $derived(colorFx[deckId]);
	const colorTint = $derived(COLOR_FX_TINT[colorSettings.effect]);
	/** MIXUX-02, the maintainer verbatim: "Filter dial should be white bg instead of black for clarity." */
	const COLOR_FX_FILTER_FACE = 'var(--rb-color-fx-filter-face)';
	const colorFace = $derived(colorSettings.effect === 'filter' ? COLOR_FX_FILTER_FACE : undefined);
	/** COLORFX-8341: Cmd-click on a Mac, Ctrl-click elsewhere. */
	const cycleModifier = quickPickModifier(typeof navigator === 'undefined' ? '' : navigator.platform);
	const cycleHint = `${cycleModifier === 'Meta' ? 'Cmd' : 'Ctrl'}-click cycles FILTER, SPACE, ECHO; switching returns the knob to 12 o'clock`;
	/** Hover title for the selector, naming the echo's timing as numbers and the knob as a clock time (DIALCLOCK-8341). */
	const colorFxTitle = $derived.by(() => {
		const label = COLOR_FX_LABELS[colorSettings.effect];
		const at = `knob at ${dialClockLabel(filter)}; ${cycleHint}`;
		if (colorSettings.effect !== 'echo') return `Color FX: ${label}, ${at}`;
		const bpm = colorSettings.tempo_bpm;
		if (bpm === null) return `Color FX: ECHO ${colorSettings.echo_beats} beat - inert: this deck has no tempo; ${at}`;
		const ms = Math.round((60 / bpm) * colorSettings.echo_beats * 1000);
		return `Color FX: ECHO ${colorSettings.echo_beats} beat at ${bpm.toFixed(1)} BPM = ${ms} ms per repeat; ${at}`;
	});

	// ------------------------------------------------- color FX menu (COLORFX-10)

	/** Chevron (7px) plus its 2px gap after the caption text, for the caption fit. */
	const COLOR_FX_CHEVRON_TRAIL_PX = 9;
	let colorFxMenu = $state(COLOR_FX_MENU_CLOSED);
	let colorFxMenuEl = $state<HTMLElement | null>(null);
	let colorFxTriggerEl = $state<HTMLElement | null>(null);
	/** Viewport position of the open menu: fixed, so the strip's overflow clip cannot cut it. */
	let colorFxMenuPos = $state({ left: 0, top: 0 });

	/** Read-only on the event: no preventDefault / stopPropagation, so the press
	 * that closes the menu still starts a drag on whatever knob is under it. */
	function handleDocumentPointerDown(event: PointerEvent): void {
		const where = classifyColorFxPress(event.target as Node | null, colorFxMenuEl, colorFxTriggerEl);
		colorFxMenu = colorFxMenuOnPointerDown(colorFxMenu, where);
	}

	function handleColorFxTap(): void {
		colorFxMenu = colorFxMenuOnTap(colorFxMenu);
		if (!colorFxMenu.open || colorFxTriggerEl === null) return;
		const rect = colorFxTriggerEl.getBoundingClientRect();
		colorFxMenuPos = { left: rect.left + rect.width / 2, top: rect.bottom + 2 };
	}

	function chooseColorFx(effect: ColorFxEffect): void {
		colorFxMenu = colorFxMenuClose();
		if (effect !== colorSettings.effect) oncolorfx(effect);
	}

	/** COLORFX-10320: a division pick sets echo_beats only; the knob stays where it is. */
	function chooseEchoDivision(beats: ColorFxEchoBeats): void {
		colorFxMenu = colorFxMenuClose();
		if (beats !== colorSettings.echo_beats) onechodivision(beats);
	}

	function handleDocumentKeyDown(event: KeyboardEvent): void {
		if (event.key === 'Escape' && colorFxMenu.open) colorFxMenu = colorFxMenuClose();
	}

	$effect(() => {
		// Capture phase: sees every press first, including ones a child handler
		// stops later. It only reads the event.
		document.addEventListener('pointerdown', handleDocumentPointerDown, true);
		document.addEventListener('keydown', handleDocumentKeyDown);
		return () => {
			document.removeEventListener('pointerdown', handleDocumentPointerDown, true);
			document.removeEventListener('keydown', handleDocumentKeyDown);
		};
	});

	/** STEMDIAL-DRAG-01: the slot also holds the DRUMS dial, whose presses are its own. */
	function pressOnColorKnob(event: Event): boolean {
		return colorFxTriggerEl !== null && event.target instanceof Node && colorFxTriggerEl.contains(event.target);
	}

	/** COLORFX-8341: a modifier press on the color knob is the cycle gesture, so it
	 * never also turns the knob (Knob's drag) or opens the native select. */
	function handleColorSlotPointerDown(event: PointerEvent): void {
		if (!isColorFxCycleGesture(event, cycleModifier) || !pressOnColorKnob(event)) return;
		event.preventDefault();
		event.stopPropagation();
	}
	function handleColorSlotMouseDown(event: MouseEvent): void {
		if (!isColorFxCycleGesture(event, cycleModifier) || !pressOnColorKnob(event)) return;
		event.preventDefault(); // a native select opens on mousedown
		event.stopPropagation();
	}
	function handleColorSlotClick(event: MouseEvent): void {
		if (!isColorFxCycleGesture(event, cycleModifier) || !pressOnColorKnob(event)) return;
		event.preventDefault();
		event.stopPropagation(); // not also a MIXUX-08 deck select
		oncolorfx(COLOR_FX_NEXT);
	}

	function handleStripClick(event: MouseEvent): void {
		clickSelect(deckId, event.shiftKey);
	}

	/** MIXUX-03 (the maintainer, Mon 5 Oct 2026, revised twice that day): TRIM at
	 * 80% of the 30px EQ dial. MORE only - pin 246b0f5's LESS mode uses the
	 * smaller LESS_TRIM_SIZE below. */
	const TRIM_SIZE = 24;

	// ------------------------------------------------------- level calibration (#1475)

	/** Off -> click (re)captures this channel's current tap level and arms it.
	 * On -> click disarms, keeping the captured number per prefs.svelte's
	 * `setLevelCalibrationDisabled` contract. */
	function handleCalibrationClick(kind: 'red' | 'ceiling'): void {
		const enabled =
			kind === 'red'
				? uiPrefs.level_calibration.red_enabled
				: uiPrefs.level_calibration.ceiling_enabled;
		if (enabled) {
			setLevelCalibrationDisabled(kind);
			return;
		}
		// A stopped deck is refused on TRANSPORT STATE, not just on level. The
		// numeric floor alone was not enough: the meter's PPM ballistics decay
		// at ~11.8 dB/s, so for several seconds after a pause the tap still
		// reads a real-looking value on its way down. Capturing mid-decay arms a
		// ceiling from a number that describes nothing, and a quiet tail arms a
		// severe master attenuation. `playing` is the only signal that says the
		// level means something right now.
		if (!playing) return;
		// R is captured from the value the channel meter draws (its post-crossfader
		// heard ballistics), M from the tap (Sol P1 and Codex P2, PR #6829).
		const db = calibrationCaptureDb(kind, peekDeckMeterReading(deckId));
		// Belt and braces: a playing deck can still sit at the floor (silence in
		// the track, or a graph not yet producing), and a -60 dBFS ceiling is a
		// 0.001 master multiplier, i.e. one click silences the output.
		if (db <= METER_FLOOR_DBFS) return;
		setLevelCalibrationCapture(kind, db);
	}

	/** Every numeric readout carries a title explaining the number (house rule). */
	function calibrationTitle(label: string, dbfs: number | null, enabled: boolean): string {
		if (dbfs === null) {
			return (
				`${label}: not captured. Click while a loud passage plays on this channel. ` +
				`A stopped or silent channel captures nothing: there is no level to calibrate against.`
			);
		}
		const action = enabled ? 'Click to disable (keeps the captured level).' : 'Click to re-capture and enable.';
		return `${label}: ${dbfs.toFixed(1)} dBFS, captured at this channel's tap, currently ${enabled ? 'ON' : 'OFF'}. ${action}`;
	}

	const redTitle = $derived(
		calibrationTitle(
			'Meter red anchor',
			uiPrefs.level_calibration.red_dbfs,
			uiPrefs.level_calibration.red_enabled
		)
	);
	const ceilingTitle = $derived(
		calibrationTitle(
			'Master output ceiling',
			uiPrefs.level_calibration.ceiling_dbfs,
			uiPrefs.level_calibration.ceiling_enabled
		)
	);
	const chNumTitle = $derived(`Mixer channel ${deckId} (deck ${deckId})`);
	const cueTitle = $derived(
		cueEnabled
			? `Headphone cue ON for channel ${deckId} - click to stop sending this channel to the headphones`
			: `Headphone cue OFF for channel ${deckId} - click to hear this channel in the headphones`
	);
	/** Pin 246b0f5 LESS mode: decks 1/2's strip has to fit inside the
	 * shrunk LESS deck-area row (see +page.svelte), so TRIM/EQ/FILTER all
	 * shrink - channel-strip-less-floor.test.mjs derives the LESS
	 * deck-area floor from these exact numbers, so a change here must stay
	 * in step with that test.
	 *
	 * Pin 2917b0eca218 - the maintainer's words: "in LESS (2 deck) - channel slider
	 * and butons just below it should probably go Left and Right (around)
	 * the EQs. Cant currently see filter in LESS mode." FILTER used to be
	 * unmounted here because a single vertical stack could not afford its
	 * height. `.strip.less` is now a GRID that puts the fader left of the
	 * EQs and STEM right of them, so the column the dials live in is the
	 * only one that has to be tall, and FILTER fits back into it at the
	 * same shrunk dial size as TRIM and the EQs. */
	const LESS_TRIM_SIZE = 18;
	const LESS_EQ_SIZE = 18;
	/** Knob's own default dial size (see Knob.svelte's `size = 30`), spelled out
	 * explicitly here rather than omitted: `exactOptionalPropertyTypes` treats an
	 * explicit `size={undefined}` as distinct from the prop being absent, so
	 * `eqSize` must always resolve to a concrete number, same as `trimSize`. */
	const EQ_SIZE = 30;
	const trimSize = $derived(less ? LESS_TRIM_SIZE : TRIM_SIZE);
	const eqSize = $derived(less ? LESS_EQ_SIZE : EQ_SIZE);
	/** MIXUX-12 (the maintainer, Tue 6 Oct 2026: "let's make the filter/color knob the
	 * same size as trim"): FILTER is TRIM's size in both views, which supersedes
	 * MIXUX-03's 120% FILTER. channel-strip-less-floor.test.mjs reads this line
	 * to size FILTER in the MORE and LESS floors. */
	const filterSize = $derived(trimSize);

	const stemDials = $derived(channelShowsStemDials(mixerStems, deck.stems));
	const stemLayout = $derived(stemDialLayout(deck.stems.available_controls));

	function stemAccessibleLabel(stem: StemControl): string {
		return `${STEM_DIAL_LABELS[stem].toLowerCase()} stem level deck ${deckId}`;
	}
</script>

<div
	class="strip"
	class:secondary
	class:less
	class:deck-focus={focused}
	class:selected={selected}
	data-mixer-channel={deckId}
	use:selectorNode={{ id: selId.channel(deckId), kind: 'group', enabled: channelSelectable }}
	onclick={handleStripClick}
	data-mixer-stems={stemDials}
	role="group"
	aria-label={`channel ${deckId}`}
	onpointerenter={() => setHoveredDeck(deckId)}
	onpointerleave={() => {
		if (deckHoverUi.deckId === deckId) setHoveredDeck(null);
	}}
>
	<div class="strip-head">
		<span class="ch-num" title={chNumTitle}>{deckId}</span>
		<div class="cal-controls" role="group" aria-label={`level calibration channel ${deckId}`}>
			<button
				type="button"
				class="cal-btn"
				class:active={uiPrefs.level_calibration.red_enabled}
				aria-pressed={uiPrefs.level_calibration.red_enabled}
				aria-label={`meter red anchor channel ${deckId}`}
				title={redTitle}
				onclick={() => handleCalibrationClick('red')}>R</button
			>
			<button
				type="button"
				class="cal-btn"
				class:active={uiPrefs.level_calibration.ceiling_enabled}
				aria-pressed={uiPrefs.level_calibration.ceiling_enabled}
				aria-label={`master ceiling channel ${deckId}`}
				title={ceilingTitle}
				onclick={() => handleCalibrationClick('ceiling')}>M</button
			>
		</div>
	</div>
	<div class="trim-slot" use:selectorNode={{ id: selId.channelPart(deckId, 'trim'), kind: 'leaf', enabled: true }}>
		<Knob
			knobId={knobId(deckId, 'trim')}
			label="TRIM"
			accessibleLabel={`trim deck ${deckId}`}
			value={trim}
			tone="white"
			size={trimSize}
			onchange={ontrim}
		/>
		<AutoGainReadout {deckId} onoffset={onAutoGainOffset} />
	</div>
	{#snippet stemDial(stem: StemControl, size: number)}
		<div class="sel-node" use:selectorNode={{ id: selId.channelStem(deckId, stem), kind: 'leaf', enabled: true }}>
			<Knob
				knobId={knobId(deckId, stemKnobRole(stem))}
				label={STEM_DIAL_LABELS[stem]}
				accessibleLabel={stemAccessibleLabel(stem)}
				value={deck.stems.controls[stem].gain}
				{size}
				accentColor={stemCssColor(stem)}
				onchange={(value) => {
					// Write the gain here, in the same turn as the pointer, so the
					// dial is not waiting on stem_gain to come back around the
					// command queue. The command still owns the audio graph.
					deck.stems.controls[stem].gain = value;
					onStemGain(stem, value);
				}}
			/>
		</div>
	{/snippet}
	<!-- Grid flows down a column then across: HI MID LOW, then VOCAL INST BASS
	     beside them row for row. An absent stem keeps its cell empty. -->
	<div class="eq-stack" class:with-stems={stemDials}>
		<div class="sel-node" use:selectorNode={{ id: selId.channelPart(deckId, 'high'), kind: 'leaf', enabled: true }}>
			<Knob
				knobId={knobId(deckId, 'high')}
				label="HI"
				accessibleLabel={`high EQ deck ${deckId}`}
				value={eqHigh}
				size={eqSize}
				onchange={(value) => oneq('high', value)}
			/>
		</div>
		<div class="sel-node" use:selectorNode={{ id: selId.channelPart(deckId, 'mid'), kind: 'leaf', enabled: true }}>
			<Knob
				knobId={knobId(deckId, 'mid')}
				label="MID"
				accessibleLabel={`mid EQ deck ${deckId}`}
				value={eqMid}
				size={eqSize}
				onchange={(value) => oneq('mid', value)}
			/>
		</div>
		<div class="sel-node" use:selectorNode={{ id: selId.channelPart(deckId, 'low'), kind: 'leaf', enabled: true }}>
			<Knob
				knobId={knobId(deckId, 'low')}
				label="LOW"
				accessibleLabel={`low EQ deck ${deckId}`}
				value={eqLow}
				size={eqSize}
				onchange={(value) => oneq('low', value)}
			/>
		</div>
		{#if stemDials}
			{#each stemLayout.column as stem, row (row)}
				{#if stem === null}
					<span class="stem-dial-empty" aria-hidden="true"></span>
				{:else}
					{@render stemDial(stem, eqSize)}
				{/if}
			{/each}
		{/if}
	</div>
	<!-- COLORFX-09: on SPACE / ECHO the color knob is a Web-Audio-only control, so
	     Rust engine mode holds it inert with the PARITY-TODO title instead of
	     letting it drive the Rust FILTER. On FILTER the knob stays live there.
	     STEMDIAL-DRAG-01: the hold sits on the knob, not the slot, so the DRUMS
	     dial beside it (stem_gain, which the engine plays) still turns. -->
	<!-- COLORFX-8341: Cmd-click (Ctrl-click off a Mac) on the color knob, caption
	     included, cycles to the next effect (a plain tap still opens the COLORFX-10 menu); the same `color_fx {effect: next}`
	     an agent sends. -->
	<div
		class="filter-slot"
		data-testid={`color-fx-slot-${deckId}`}
		onpointerdowncapture={handleColorSlotPointerDown}
		onmousedowncapture={handleColorSlotMouseDown}
		onclickcapture={handleColorSlotClick}
		role="group"
		aria-label={`color FX deck ${deckId}`}
	>
		<!-- COLORFX-10: a tap on the dial or its caption opens the effect menu, a
		     drag still turns the knob. The chevron sits IN the caption (in flow),
		     so the caption's width counts it and it cannot hang over a neighbour. -->
		<span
			class="filter-knob"
			data-rust-command={colorSettings.effect === 'filter' ? undefined : 'color_fx'}
			bind:this={colorFxTriggerEl}
			use:selectorNode={{ id: selId.channelPart(deckId, 'filter'), kind: 'leaf', enabled: true }}
			data-testid={`color-fx-trigger-${deckId}`}
		>
			<Knob
				knobId={knobId(deckId, 'filter')}
				label={COLOR_FX_LABELS[colorSettings.effect]}
				accessibleLabel={`filter deck ${deckId}`}
				value={filter}
				size={filterSize}
				{...(colorTint ? { accentColor: colorTint } : {})}
				{...(colorFace ? { faceColor: colorFace } : {})}
				onchange={onfilter}
				ontap={handleColorFxTap}
				popupOpen={colorFxMenu.open}
				captionTrail={colorFxChevron}
				captionTrailPx={COLOR_FX_CHEVRON_TRAIL_PX}
			/>
		</span>
		{#if colorFxMenu.open}
			<div
				class="color-fx-menu"
				role="menu"
				aria-label={`color FX type deck ${deckId}`}
				title={colorFxTitle}
				data-testid={`color-fx-menu-${deckId}`}
				style={`left: ${colorFxMenuPos.left}px; top: ${colorFxMenuPos.top}px;`}
				bind:this={colorFxMenuEl}
			>
				{#each COLOR_FX_EFFECTS as effect (effect)}
					<button
						type="button"
						role="menuitemradio"
						class="color-fx-option"
						class:checked={colorSettings.effect === effect}
						aria-checked={colorSettings.effect === effect}
						data-testid={`color-fx-option-${deckId}-${effect}`}
						data-rust-command="color_fx"
						onclick={() => chooseColorFx(effect)}>{COLOR_FX_LABELS[effect]}</button
					>
				{/each}
				{#if showsEchoDivisions(colorSettings.effect)}
					<div class="color-fx-divisions" role="group" aria-label={`echo division deck ${deckId}`} data-testid={`color-fx-divisions-${deckId}`}>
						{#each COLOR_FX_DIVISIONS as beats (beats)}
							<button
								type="button"
								role="menuitemradio"
								class="color-fx-option"
								class:checked={colorSettings.echo_beats === beats}
								aria-checked={colorSettings.echo_beats === beats}
								title={echoDivisionTitle(beats, colorSettings.tempo_bpm)}
								data-testid={`color-fx-division-${deckId}-${beats}`}
								data-rust-command="color_fx"
								onclick={() => chooseEchoDivision(beats)}>{COLOR_FX_DIVISION_LABELS[beats]}</button
							>
						{/each}
					</div>
				{/if}
			</div>
		{/if}
		{#if stemDials && stemLayout.filterPartner !== null}
			{@render stemDial(stemLayout.filterPartner, filterSize)}
		{/if}
	</div>
	<button
		class:enabled={cueEnabled}
		class="cue-btn"
		aria-pressed={cueEnabled}
		aria-label={`cue channel ${deckId}`}
		title={cueTitle}
		data-testid={`cue-channel-${deckId}`}
		data-rust-command="channel_cue"
		onclick={() => oncue(!cueEnabled)}
	>
		<svg class="cue-icon" width="9" height="9" viewBox="0 0 12 12" aria-hidden="true">
			<!-- headphone band + ear cups, matching the monitor cluster's icon -->
			<path d="M2 8 V6 a4 4 0 0 1 8 0 v2" fill="none" stroke="currentColor" stroke-width="1.4" />
			<rect x="1" y="7" width="2.4" height="3.4" rx="0.8" fill="currentColor" />
			<rect x="8.6" y="7" width="2.4" height="3.4" rx="0.8" fill="currentColor" />
		</svg>CUE</button
	>
	<div class="fader-slot" use:selectorNode={{ id: selId.channelPart(deckId, 'fader'), kind: 'leaf', enabled: true }}>
		<VFader
			value={fader}
			{playing}
			{looped}
			deckId={deckId}
			onchange={onfader}
			label={`channel fader deck ${deckId}`}
		/>
	</div>
	<span class="stem-label" title="Stem mute (click) and solo (Shift+click) for this channel">STEM</span>
	<div class="stem-slot">
		<StemRow
			deck={deck}
			pending={stemPending}
			testIdScope="channel"
			onMute={onStemMute}
			onSolo={onStemSolo}
			stemsResetOn={trackLoadResetEnabled('stems')}
			onOpenResetConfig={(el) => toggleTrackLoadResetPanel('stems', el, deck.deck_id)}
		/>
	</div>
</div>

{#snippet colorFxChevron()}
	<span class="color-fx-chevron" title={colorFxTitle} data-testid={`color-fx-chevron-${deckId}`}>
		<svg width="7" height="5" viewBox="0 0 7 5" aria-hidden="true">
			<path d="M0.5 0.5 L3.5 4 L6.5 0.5" fill="none" stroke="currentColor" stroke-width="1.2" />
		</svg>
	</span>
{/snippet}

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
		padding: 2px 2px 6px;
		border-radius: 2px;
	}
	/* Pin 2917b0eca218: "channel slider and butons just below it should
	 * probably go Left and Right (around) the EQs. Cant currently see filter
	 * in LESS mode."
	 *
	 * LESS mode only. The DOM is unchanged - MORE keeps the flex column
	 * above - and the three columns come from grid AREAS, so no child moves
	 * in the markup and every selector, testid and hotkey target elsewhere
	 * still resolves. Measured at 1280x800 the strip is 114px wide and the
	 * three columns need about 30 + 22 + 41 = 93px, so this fits across;
	 * the win is vertical, where the single stack needed 299px and the
	 * tallest column here needs about half that - which is what buys FILTER
	 * its place back and lets +page.svelte hand the difference to the
	 * library instead.
	 *
	 * The fader spans both dial rows on purpose: it is the one control that
	 * should absorb spare height (`.fader-slot` is `flex: 1 1 auto` in MORE
	 * for exactly that reason), so in LESS it comes out TALLER than the
	 * 67px it used to get, not shorter.
	 *
	 * LESSV-01 (the maintainer, Tue 6 Oct 2026): "The deck heights should be the same as
	 * for MORE. Only the central mixer needs re-arranging to keep it all
	 * fitting vertically." The deck-area row in LESS is now one MORE deck tall
	 * (249px floor, see +page.svelte), which the old four-row grid (173px of
	 * strip) could not fit beside the toggle and the headphone/crossfader
	 * rows. So TRIM, CUE and FILTER moved into a column RIGHT of the HI/MID/LOW
	 * stack (TRIM level with HI, FILTER level with LOW, the order they read in
	 * MORE), and the STEM chips moved under everything as one full-width row
	 * (wrapping to a second line when a bundle carries all five stems), with
	 * their STEM mode label top right, beside the channel number. The dial
	 * block is now the only tall thing, and nothing is unmounted. */
	.strip.less {
		display: grid;
		grid-template-columns: max-content max-content max-content;
		grid-template-rows: auto auto auto 1fr auto;
		grid-template-areas:
			'head head stemlabel'
			'fader eq trim'
			'fader eq cue'
			'fader eq filter'
			'stem stem stem';
		justify-content: center;
		align-items: start;
		justify-items: center;
		column-gap: 4px;
	}
	.strip.less .strip-head {
		grid-area: head;
	}
	/* LESSV-02: the R|M calibration buttons are hidden in LESS, not removed.
	 * They stay mounted (and stay in MORE), so the prefs they drive and
	 * every other path to them are untouched. */
	.strip.less .cal-controls {
		display: none;
	}
	.strip.less .eq-stack {
		grid-area: eq;
	}
	.strip.less .filter-slot {
		grid-area: filter;
	}
	.strip.less .stem-slot {
		grid-area: stem;
		justify-self: stretch;
		align-self: center;
		min-width: 0;
	}
	.strip.less .stem-slot :global(.stems) {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 2px;
	}
	.strip.secondary {
		background: color-mix(in srgb, var(--rb-panel-raised, #1a1e25) 55%, transparent);
	}
	/* DECKUX-5917: top-edge tick, like a channel-assign light. */
	.strip.deck-focus {
		box-shadow: var(--rb-deck-link-tick-top);
	}
	.strip.selected {
		box-shadow: inset 0 0 0 0.5px rgba(255, 255, 255, 0.13);
	}
	.strip.selected.deck-focus {
		box-shadow:
			var(--rb-deck-link-tick-top),
			inset 0 0 0 0.5px rgba(255, 255, 255, 0.13);
	}
	.strip-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		gap: 2px;
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
	/* `flex-shrink` is inert under `.strip.less`'s grid (pin 2917b0eca218),
	 * and that is fine rather than a gap: a grid item's default
	 * `min-height: auto` already refuses to shrink below its content, so a
	 * too-short LESS strip overflows visibly exactly as the flex column did.
	 * Nothing here may set `min-height: 0` on those items without restoring
	 * an equivalent refusal. */
	.ch-num {
		font-size: 10px;
		color: var(--rb-text);
		line-height: 1;
	}
	/* #1475: R/M by-ear calibration - OTT, takes no space. */
	.cal-controls {
		display: flex;
		gap: 1px;
	}
	.cal-btn {
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 7px;
		line-height: 1;
		padding: 1px 3px;
		cursor: pointer;
	}
	.cal-btn.active {
		color: var(--rb-accent);
		border-color: var(--rb-accent);
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
		position: relative;
		margin-bottom: 3px;
	}
	/* Pin 246b0f5 LESS mode: FILTER (the inert stub below) drops out of the
	 * layout entirely, so the remaining vertical margins tighten further -
	 * channel-strip-less-floor.test.mjs derives the LESS deck-area floor
	 * from these exact numbers, so a change here must stay in step with
	 * that test. */
	.strip.less .trim-slot {
		grid-area: trim;
		margin-bottom: 3px;
	}
	/* Rows are HI/MID/LOW; with the S button on a second column of stem dials
	 * stands beside them. Same row height either way, so the strip floors in
	 * channel-strip-less-floor.test.mjs hold with stems on or off. */
	.eq-stack {
		display: grid;
		grid-template-rows: repeat(3, auto);
		grid-auto-flow: column;
		justify-items: center;
		align-items: center;
		gap: 3px 4px;
	}
	.stem-dial-empty {
		display: block;
	}
	.filter-slot {
		display: flex;
		align-items: flex-start;
		gap: 4px;
		margin-top: 1px;
		margin-bottom: 3px;
		position: relative;
	}
	/* The FILTER knob and its in-caption effect chevron, kept together so the
	   S view's DRUMS dial can sit beside them in the same slot (COLORFX-10:
	   the chevron is in flow, so it can no longer hang over that dial). */
	.filter-knob {
		display: flex;
		justify-content: center;
	}
	/* COLORFX-10: in flow after the caption text (Knob's captionTrail), 2px gap. */
	.color-fx-chevron {
		display: inline-flex;
		flex: none;
		margin-left: 2px;
		color: var(--rb-text-dim);
	}
	/* position: fixed from the trigger's rect: the strip slot clips overflow. */
	.color-fx-menu {
		position: fixed;
		transform: translateX(-50%);
		z-index: 50;
		display: flex;
		flex-direction: column;
		min-width: 44px;
		padding: 2px;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 4px 10px rgba(0, 0, 0, 0.45);
	}
	.color-fx-divisions {
		display: flex;
		flex-direction: column;
		border-top: 1px solid rgb(255 255 255 / 0.12);
		margin-top: 2px;
		padding-top: 2px;
	}

	.color-fx-option {
		background: transparent;
		border: 0;
		border-radius: 2px;
		padding: 3px 6px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 8px;
		letter-spacing: 0.04em;
		line-height: 1;
		text-align: left;
		cursor: pointer;
	}
	.color-fx-option:hover,
	.color-fx-option:focus-visible {
		background: color-mix(in srgb, var(--rb-accent) 22%, transparent);
		color: var(--rb-text);
	}
	.color-fx-option.checked {
		color: var(--rb-accent);
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
		/* The icon sits to the LEFT of the word, inside the button, so a glance
		   reads "this sends the channel to the headphones" without the label. */
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 3px;
	}
	.cue-icon {
		flex: 0 0 auto;
	}
	.strip.less .cue-btn {
		grid-area: cue;
		margin-bottom: 4px;
	}
	.cue-btn.enabled {
		color: var(--rb-accent);
		border-color: var(--rb-accent);
	}
	.fader-slot {
		flex: 1 1 auto;
		/* Pin 8cd32a28c36d: reclaim vertical budget for STEM separation below. */
		min-height: 58px;
		width: 100%;
		display: flex;
		justify-content: center;
		align-items: stretch;
		margin-top: 0;
		margin-bottom: 2px;
	}
	.strip.less .fader-slot {
		grid-area: fader;
		align-self: stretch;
		margin-bottom: 4px;
	}
	.stem-label {
		font-size: 8px;
		letter-spacing: 0.06em;
		color: var(--rb-text-dim);
		line-height: 1;
		flex: none;
		font-family: var(--rb-font);
		/* Pin 8cd32a28c36d: clearer gap above the STEM row (was 2px). */
		margin-top: 4px;
	}
	.strip.less .stem-label {
		grid-area: stemlabel;
		align-self: center;
		margin-top: 0px;
	}
	.stem-slot {
		flex: 0 0 auto;
		align-self: stretch;
	}
	.stem-slot :global(.stems) {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 2px;
	}
	.stem-slot :global(.mute) {
		/* The STEM heading already identifies this compact mixer group. */
		display: none;
	}
	.stem-slot :global(.chip) {
		font-size: 7px;
		padding: 1px;
	}
	/* Same row as the chips. Fills the spare grid cell instead of adding a row. */
	.stem-slot :global(.reset-cfg) {
		font-size: 7px;
		padding: 1px 2px;
		min-width: 0;
	}
</style>
