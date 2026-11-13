<script lang="ts">
	// Real precomputed Demucs controls. Click toggles mute; Shift+click toggles
	// solo; scroll sets the stem level; double-click returns it to unity
	// (STEMPERF-11699). All paths dispatch through the typed browser-agent
	// command surface. STEMPERF-11695: ACA / INST / ALL quick chips (deck only)
	// plan the same commands.
	import type { DeckState } from '$lib/rb/deck-state-types';
	import {
		createChipClickArbiter,
		stemChipDbLabel,
		stemChipLevelView,
		stemChipWheelParams
	} from '$lib/rb/stem-chip-level';
	// A fill only shows while the stem-set loads, and !ready already disables the chip then.
	import { stemChipFill, stemChipFillAttr } from '$lib/rb/stem-chip-fill';
	import { stemCssColor } from '$lib/rb/stem-colors';
	import type { SeparateActionView } from '$lib/rb/stem-separate-view';
	import { stemStatusView } from '$lib/rb/stem-status';
	import { quickModeActive, type StemQuickMode } from '$lib/rb/stem-quick-mode';
	import type { StemControl } from '$lib/rb/stem-types';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import { optionalSelectorNode, stemRowLeaf } from './deck-stem-selector-ids';

	let {
		deck,
		pending,
		testIdScope = 'deck',
		onMute,
		onSolo,
		onGain,
		onLoad,
		separate = null,
		onSeparate,
		stemsResetOn = false,
		onOpenResetConfig = () => {},
		onQuickMode,
		selectorPrefix = null
	}: {
		deck: DeckState;
		pending: boolean;
		/** Namespace for chip testids: `deck` keeps `stem-*-deck-N`; mixer uses `channel`. */
		testIdScope?: 'deck' | 'channel';
		onMute: (stem: StemControl) => Promise<void>;
		onSolo: (stem: StemControl) => Promise<void>;
		/** STEMPERF-11699: wheel over a live chip sets that stem's level; double-click returns it to unity. */
		onGain: (stem: StemControl, value: number) => void;
		/** STEM-48: retry a failed stem load, or start one that is held. The deck
		 * passes it; the mixer strip does not, and shows the reason on hover only. */
		onLoad?: () => Promise<void>;
		/** STEM-77: the "Separate this track" action on a NO STEMS deck, or null. */
		separate?: SeparateActionView | null;
		onSeparate?: () => Promise<void>;
		/** The track-load reset chip is armed (the stems reset is on). Owned by the parent, read from the reset state. */
		stemsResetOn?: boolean;
		/** Opens the track-load reset panel from the chip, anchored on the chip element. */
		onOpenResetConfig?: (el: HTMLElement) => void;
		/** STEMPERF-11695: apply an ACA / INST / ALL quick mode. The deck passes it;
		 * the mixer strip does not, so its row stays narrow. */
		onQuickMode?: (mode: StemQuickMode) => Promise<void>;
		/** #14471: selector group id for one leaf per button (the deck passes `deck.<n>.stems`);
		 * null registers no selector nodes (the mixer strip registers its own stem leaves). */
		selectorPrefix?: string | null;
	} = $props();

	const QUICK: readonly { mode: StemQuickMode; label: string; tip: string }[] = [
		{ mode: 'aca', label: 'ACA', tip: 'Acapella: only VOCAL plays' },
		{ mode: 'inst', label: 'INST', tip: 'Instrumental: VOCAL muted, every solo cleared' },
		{ mode: 'all', label: 'ALL', tip: 'Everything: every mute and solo cleared, gains kept' }
	];
	const quickActive = $derived(quickModeActive(deck.stems));

	const STEMS: readonly { id: StemControl; label: string }[] = [
		{ id: 'vocal', label: 'VOCAL' },
		{ id: 'instrumental', label: 'INST' },
		{ id: 'drums', label: 'DRUMS' },
		{ id: 'bass', label: 'BASS' },
		{ id: 'other', label: 'HARM' }
	];
	const visibleStems = $derived(STEMS.filter((stem) =>
		(stem.id !== 'bass' && stem.id !== 'other') || deck.stems.available_controls.includes(stem.id)));
	const ready: boolean = $derived(deck.stems.status === 'ready');
	function openResetConfig(event: MouseEvent): void {
		event.stopPropagation();
		const el = event.currentTarget;
		if (el instanceof HTMLElement) onOpenResetConfig(el);
	}

	/** A control this bundle's layout cannot drive. RoFormer's 2-stem split
	 * folds the drums into `instrumental`, so DRUMS has no signal of its own:
	 * the chip renders inert with a reason rather than accepting a click that
	 * could never change what you hear. */
	function unavailable(stem: StemControl): boolean {
		return ready && !deck.stems.available_controls.includes(stem);
	}

	/** STEM-48: the one named reading of the stem state. A chip that is not
	 * live always carries this reason on hover, and the deck shows its label. */
	const view = $derived(stemStatusView(deck.stems));
	const statusTip: string = $derived(view.tip);

	function chipTip(stem: StemControl, label: string): string {
		if (unavailable(stem)) {
			return (
				`${label} is not a separate stem in this ${deck.stems.layout ?? 'bundle'} ` +
				`- it is mixed into INST, so it cannot be muted on its own`
			);
		}
		const ctrl = deck.stems.controls[stem];
		const state = ctrl.solo ? 'solo selected' : ctrl.muted ? 'muted now' : 'unmuted (other solo/group controls still apply)';
		const level = stemChipDbLabel(ctrl.gain);
		return `${label} stem is ${state}, level ${level}. Click to mute, Shift+click to solo, scroll for level, double-click for unity. ${statusTip}`;
	}

	// Every click dispatches at once; a double-click's two clicks cancel out, so the dblclick only resets gain.
	const arbiter = createChipClickArbiter({
		onMute: (stem) => onMute(stem),
		onSolo: (stem) => onSolo(stem),
		onGain: (stem, value) => onGain(stem, value)
	});

	function toggle(event: MouseEvent, stem: StemControl): void {
		arbiter.click({ stem, shift: event.shiftKey });
	}
</script>

	<div class="stems" role="group" aria-label={`stem controls deck ${deck.deck_id}`} data-stems-status={deck.stems.status} data-stems-state={view.name} data-stems-phase={deck.stems.load?.phase ?? null} title={statusTip}>
	<span class="mute">MUTE</span>
	{#each visibleStems as stem (stem.id)}
		{@const level = stemChipLevelView(deck.stems.controls[stem.id].gain)}
		{@const fill = stemChipFill(deck.stems, stem.id)}
		<div class="sel-leaf" use:optionalSelectorNode={stemRowLeaf(selectorPrefix, stem.id, ready && !unavailable(stem.id))}>
		<button
			class="chip"
			class:muted={deck.stems.controls[stem.id].muted}
			class:solo={deck.stems.controls[stem.id].solo}
			class:unavailable={unavailable(stem.id)}
			disabled={!ready || unavailable(stem.id)}
			data-fill={stemChipFillAttr(fill)}
			aria-busy={pending}
			title={chipTip(stem.id, stem.label)}
			aria-label={`${stem.label} stem mute deck ${deck.deck_id}; Shift+click solo`}
			data-testid={`stem-${stem.id}-${testIdScope}-${deck.deck_id}`}
			data-unavailable={unavailable(stem.id)}
			aria-pressed={deck.stems.controls[stem.id].muted}
			data-performance-control={`stem-${stem.id}`}
			data-muted={deck.stems.controls[stem.id].muted}
			data-solo={deck.stems.controls[stem.id].solo}
			data-stem-gain={deck.stems.controls[stem.id].gain}
			style={`--chip-color:${stemCssColor(stem.id)}`}
			onclick={(event) => toggle(event, stem.id)}
			ondblclick={() => arbiter.doubleClick(stem.id)}
			use:wheelAdjust={stemChipWheelParams({
				stem: stem.id,
				gain: () => deck.stems.controls[stem.id].gain,
				live: ready && !unavailable(stem.id),
				step: WHEEL_STEP.knob,
				onGain
			})}
		>
			{testIdScope === 'channel' ? stem.label.slice(0, 3) : stem.label}
			{#if fill === 'indeterminate'}<span class="fill sweep" aria-hidden="true"></span>{:else if fill !== null}<span class="fill" aria-hidden="true" style={`height:${fill * 100}%`}></span>{/if}
			{#if !level.unity}<span class="level" aria-hidden="true" style={`height:${level.barPx}px`}></span>{/if}
		</button>
		</div>
	{/each}
	{#if onQuickMode !== undefined}
		{#each QUICK as q (q.mode)}
			<div class="sel-leaf" use:optionalSelectorNode={stemRowLeaf(selectorPrefix, `quick.${q.mode}`, ready)}>
			<button
				type="button"
				class="quick"
				class:active={quickActive === q.mode}
				disabled={!ready}
				aria-busy={pending}
				aria-pressed={quickActive === q.mode}
				data-active={quickActive === q.mode}
				data-testid={`stem-quick-${q.mode}-${testIdScope}-${deck.deck_id}`}
				data-performance-control={`stem-quick-${q.mode}`}
				title={`${q.tip}. ${statusTip}`}
				aria-label={`${q.label} quick stem mode deck ${deck.deck_id}`}
				onclick={async () => await onQuickMode(q.mode)}
			>
				{q.label}
			</button>
			</div>
		{/each}
	{/if}
	<div class="sel-leaf" use:optionalSelectorNode={stemRowLeaf(selectorPrefix, 'reset', true)}>
	<button
		type="button"
		class="reset-cfg"
		class:armed={stemsResetOn}
		data-track-load-reset-open
		data-testid={`track-load-reset-${testIdScope}-${deck.deck_id}`}
		aria-label={`Reset level, stems, EQ when a new track loads, deck ${deck.deck_id}`}
		title="Reset level, stems, EQ, trim, or filter when a new track loads. Open DJ resets; your controller does not."
		onclick={openResetConfig}
	>
		RST
	</button>
	</div>
	{#if onLoad !== undefined && view.label !== ''}
		{#if view.action !== null}
			<div class="sel-leaf" use:optionalSelectorNode={stemRowLeaf(selectorPrefix, 'load', true)}>
			<button
				class="status action"
				class:failed={view.name === 'error'}
				title={view.tip}
				aria-label={`${view.tip} Deck ${deck.deck_id}.`}
				data-testid={`stem-status-${testIdScope}-${deck.deck_id}`}
				data-stems-state={view.name}
				data-stems-action={view.action}
				data-performance-control="stem-load"
				onclick={async () => await onLoad()}
			>
				{view.label}
			</button>
			</div>
		{:else}
			<span
				class="status"
				class:busy={view.busy}
				role="status"
				aria-live="polite"
				title={view.tip}
				data-testid={`stem-status-${testIdScope}-${deck.deck_id}`}
				data-stems-state={view.name}
			>
				{view.label}
			</span>
		{/if}
		{#if view.name === 'none' && separate !== null && onSeparate !== undefined}
			<div class="sel-leaf" use:optionalSelectorNode={stemRowLeaf(selectorPrefix, 'separate', !separate.disabled)}>
			<button
				class="status action separate"
				disabled={separate.disabled}
				title={separate.tip}
				aria-label={`${separate.tip} Deck ${deck.deck_id}.`}
				data-testid={`stem-separate-${testIdScope}-${deck.deck_id}`}
				data-performance-control="stem-separate"
				onclick={async () => await onSeparate()}
			>
				{separate.label}
			</button>
			</div>
		{/if}
	{/if}
</div>

<style>
	.stems {
		display: flex;
		align-items: center;
		gap: 4px;
		flex: 0 0 auto;
	}
	/* #14471: a selector leaf wraps exactly one button and takes no box, so the row lays out as before. */
	.sel-leaf {
		display: contents;
	}
	.mute {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		font-weight: 600;
		margin-right: 2px;
	}
	.chip {
		background: color-mix(in srgb, var(--chip-color) 15%, transparent);
		border: 1px solid var(--chip-color);
		border-radius: 2px;
		/* UICONTRAST-6037: the light theme and the Light skin set --rb-stem-ink to
		   their dark text; the stem's own tint stays on the border and fill.
		   Light VOCAL #4f6a80 on its tint measured 4.15:1. */
		color: var(--rb-stem-ink, var(--chip-color));
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		padding: 1px 8px;
		cursor: pointer;
		position: relative;
	}
	/* STEMPERF-11699: the stem level, shown only off unity; independent of mute. */
	.chip .level {
		position: absolute;
		left: 0;
		right: 0;
		bottom: 0;
		background: var(--chip-color);
		pointer-events: none;
	}
	/* #10331: stem-set fetch progress, a bottom-up tint in the chip's colour. */
	.chip .fill {
		position: absolute;
		left: 0;
		right: 0;
		bottom: 0;
		background: color-mix(in srgb, var(--chip-color) 25%, transparent);
		pointer-events: none;
	}
	.chip .fill.sweep {
		height: 100%;
		animation: stem-chip-sweep 2.4s ease-in-out infinite;
	}
	@keyframes stem-chip-sweep {
		0%, 100% { clip-path: inset(100% 0 0 0); }
		50% { clip-path: inset(0 0 0 0); }
	}
	@media (prefers-reduced-motion: reduce) {
		.chip .fill.sweep { animation: none; opacity: 0.5; }
	}
	.chip.muted {
		background: transparent;
		color: var(--rb-text-dim);
		border-color: var(--rb-border);
		text-decoration: line-through;
	}
	.chip.solo {
		box-shadow: 0 0 6px var(--chip-color);
	}
	.chip:disabled {
		background: transparent;
		color: var(--rb-text-dim);
		border-color: var(--rb-border);
		cursor: default;
		opacity: 0.6;
	}
	/* STEM-48: names what the stems are doing whenever the chips are not live.
	   Truncates rather than pushing the deck's row wider; the hover title
	   carries the whole sentence. */
	.status {
		max-width: 190px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		color: var(--rb-text-dim);
		margin-left: 4px;
	}
	/* No skin defines --rb-blue, so the light faces painted the #4aa3ff
	   fallback: 2.59:1 on the light theme's panel and 1.07:1 on its focused
	   deck (UICONTRAST-6037). They take --rb-stem-ink, their dark text. */
	.status.busy {
		color: var(--rb-stem-ink, var(--rb-blue, #4aa3ff));
	}
	.status.action {
		background: transparent;
		border: 1px solid var(--rb-blue, #4aa3ff);
		border-radius: 2px;
		/* UICONTRAST-6037: --rb-stem-ink when the skin sets it; otherwise the skin's
		   own text color so action labels clear WCAG AA in light skins (main); the
		   blue border carries the action cue. */
		color: var(--rb-stem-ink, var(--rb-text));
		padding: 1px 6px;
		cursor: pointer;
	}
	.status.action:disabled {
		border-color: var(--rb-border);
		color: var(--rb-text-dim);
		cursor: default;
	}
	.status.action.failed {
		border-color: var(--rb-red, #d0342c);
		color: #ff8e87;
	}
	/* Dimmer than merely-disabled: this control does not exist for this
	   bundle, as opposed to existing but not being ready yet. */
	.chip.unavailable {
		opacity: 0.3;
		border-style: dashed;
		cursor: not-allowed;
	}
	/* STEMPERF-11695: quick modes; the active one carries the accent. */
	.quick {
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-stem-ink, var(--rb-text));
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		padding: 1px 6px;
		cursor: pointer;
	}
	.quick.active {
		border-color: var(--rb-accent, #49c8ff);
		color: color-mix(in srgb, var(--rb-accent, #49c8ff) 60%, var(--rb-text));
	}
	.quick:disabled {
		color: var(--rb-text-dim);
		border-color: var(--rb-border);
		cursor: default;
		opacity: 0.6;
	}
	/* DECKGAP-7691 (pin 251822d6104b): opens the per-item "reset on new track" list. */
	.reset-cfg {
		background: transparent;
		border: 1px dashed var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		font-weight: 700;
		padding: 1px 4px;
		cursor: pointer;
	}
	.reset-cfg.armed {
		border-style: solid;
		border-color: var(--rb-accent, #49c8ff);
		/* WCAG AA: the raw accent reads 3.6:1 on the dark panel (ui-contrast-audit); 40 percent of the skin's own text color
		   lifts it past 4.5:1 in each skin's direction (lighter on dark skins, darker on light ones). */
		color: color-mix(in srgb, var(--rb-accent, #49c8ff) 60%, var(--rb-text));
	}
</style>
