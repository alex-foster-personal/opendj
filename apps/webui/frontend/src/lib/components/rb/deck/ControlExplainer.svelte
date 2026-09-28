<script lang="ts">
	// Hover/focus teaching chrome for performance controls. Native `title` stays
	// on the wrapped control by convention; this popover adds short bullets +
	// optional SVG demos. UI-only - does not invent engine behavior. Copy must
	// match audio-engine.
	//
	// A caller whose wrapped control would otherwise show BOTH the native
	// title and this popover at once (the overlap pin dd4f0f5ae33f flagged)
	// should drop that control's own `title` and keep only `aria-label` -
	// this component's own `title` prop still reaches screen readers via the
	// popover's heading and, on slow/no-hover, is unaffected either way.
	import { onDestroy, onMount, tick } from 'svelte';
	import type { Snippet } from 'svelte';
	import { placeFloating, type Size } from '$lib/ui/clamp-to-viewport';

	const INTERACTIVE_HIDE_DELAY_MS = 150;

	export type ExplainerDemo =
		| 'cue'
		| 'slip'
		| 'split-view'
		| 'link'
		| 'fx'
		| '2-deck-view'
		| 'headphone-mix'
		| 'headphone-mode'
		| 'headphone-practice'
		| 'headphone-split';

	let {
		title,
		bullets = [],
		warning = null,
		action = null,
		demo = null,
		placement = 'auto',
		showDelayMs = 0,
		pinOnClick = false,
		programmaticOpen = false,
		onProgrammaticClose = undefined,
		children
	}: {
		/** Native tooltip text mirrored for screen readers / slow hover. */
		title: string;
		/** Short factual bullets shown in the rich popover. */
		bullets?: readonly string[];
		/** Red, factual warning shown above an optional interactive action. */
		warning?: string | null;
		/** Optional action that must drive an existing typed UI dispatcher. */
		action?: Snippet | null;
		/** Optional mini SVG animation that teaches the control. */
		demo?: ExplainerDemo | null;
		/** Prefer above; `auto` flips below when near the top of the viewport.
		 * `right` sits beside the trigger (flipping left when it would overflow),
		 * for stacked menus where above/below would cover sibling controls. */
		placement?: 'auto' | 'above' | 'below' | 'right';
		/**
		 * Trailing show debounce in ms. Default 0 = show immediately, which
		 * preserves every existing caller's current behavior. A caller with a
		 * reason to debounce (e.g. several of these packed edge to edge, where
		 * a fast skim across them would otherwise flash a popover per item)
		 * sets its own value and states that reason at the call site.
		 */
		showDelayMs?: number;
		/** Click pins the popover open until Escape or an outside click. Native
		 * selects in the action slot need this; hover-only would close them. */
		pinOnClick?: boolean;
		/** Parent-driven pin (e.g. bottom-tray I/O entry). Opens and pins until dismissed. */
		programmaticOpen?: boolean;
		onProgrammaticClose?: (() => void) | undefined;
		children: Snippet;
	} = $props();

	let wrapEl: HTMLSpanElement | undefined = $state();
	let popEl: HTMLDivElement | undefined = $state();
	let open = $state(false);
	let pinned = $state(false);
	let popStyle = $state('');
	let hideTimer: ReturnType<typeof setTimeout> | undefined;
	let showTimer: ReturnType<typeof setTimeout> | undefined;

	const hasRich: boolean = $derived(bullets.length > 0 || warning !== null || action !== null || demo !== null);

	function _estimateSize(): Size {
		// Tall action menus (the I/O device pickers) must not be estimated as
		// a 120px tooltip: clamp then paints them over the trigger and the
		// CUEOUT-06 SHOW AUDIO I/O click hits the overlay.
		return action !== null ? { width: 280, height: 320 } : { width: 240, height: 120 };
	}

	function _place(measured: Size | null = null): void {
		if (!wrapEl) return;
		const r = wrapEl.getBoundingClientRect();
		const size = measured ?? _estimateSize();
		const viewport = { width: window.innerWidth, height: window.innerHeight };
		const box =
			placement === 'right'
				? placeFloating({
						trigger: { left: r.left, top: r.top, width: r.width, height: r.height },
						size,
						viewport,
						preferred: 'right',
						gap: 8
					})
				: placeFloating({
						trigger: {
							left: r.left + r.width / 2 - size.width / 2,
							top: r.top,
							width: size.width,
							height: r.height
						},
						size,
						viewport,
						preferred:
							placement === 'above' || (placement === 'auto' && r.top > 170) ? 'above' : 'below',
						gap: 6
					});
		popStyle = `left:${Math.round(box.x)}px;top:${Math.round(box.y)}px`;
	}

	function _placeFromPop(): void {
		if (popEl === undefined) return;
		const width = popEl.offsetWidth;
		const height = popEl.offsetHeight;
		if (width <= 0 || height <= 0) return;
		_place({ width, height });
	}

	async function _openNow(): Promise<void> {
		showTimer = undefined;
		_place();
		open = true;
		await tick();
		_placeFromPop();
	}

	function _show(): void {
		if (hideTimer !== undefined) clearTimeout(hideTimer);
		hideTimer = undefined;
		if (showTimer !== undefined) clearTimeout(showTimer);
		showTimer = undefined;
		if (!hasRich) return;
		if (showDelayMs > 0) {
			showTimer = setTimeout(_openNow, showDelayMs);
		} else {
			_openNow();
		}
	}

	function _close(): void {
		if (hideTimer !== undefined) clearTimeout(hideTimer);
		hideTimer = undefined;
		if (showTimer !== undefined) clearTimeout(showTimer);
		showTimer = undefined;
		const wasProgrammatic = pinned && programmaticOpen;
		pinned = false;
		open = false;
		if (wasProgrammatic) onProgrammaticClose?.();
	}

	function _hide(event: FocusEvent | PointerEvent): void {
		if (pinned) return;
		const next = event.relatedTarget;
		if (next instanceof Node && wrapEl?.contains(next)) return;
		if (event instanceof PointerEvent && action !== null) {
			if (hideTimer !== undefined) clearTimeout(hideTimer);
			hideTimer = setTimeout(_close, INTERACTIVE_HIDE_DELAY_MS);
			return;
		}
		_close();
	}

	function _pinFromClick(): void {
		if (!pinOnClick) return;
		pinned = true;
		_show();
	}

	function _pinPlacementArmed(): boolean {
		const g = window as unknown as Record<string, unknown>;
		const armed = g.__mdtPinPlacementArmed as { get?: () => boolean } | undefined;
		return armed?.get?.() === true;
	}

	function _onDocumentPointerDown(event: PointerEvent): void {
		if (!pinned) return;
		if (_pinPlacementArmed()) return;
		const target = event.target;
		// The press that ARMS pin placement lands before the armed flag is set,
		// so pin-arm chrome must not close a pinned menu the pin is meant for.
		if (target instanceof Element && target.closest('.fb-place-skip') !== null) return;
		if (target instanceof Node && wrapEl?.contains(target)) return;
		if (target instanceof Node && popEl?.contains(target)) return;
		// Native <select> menus paint outside the DOM. Skip only that case:
		// a select inside this explainer is focused AND the hit is the page root.
		const active = document.activeElement;
		if (
			active instanceof HTMLSelectElement &&
			wrapEl?.contains(active) &&
			target instanceof Element &&
			(target === document.documentElement || target === document.body)
		) {
			return;
		}
		_close();
	}

	function _onKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		event.preventDefault();
		wrapEl?.querySelector<HTMLElement>('button, [tabindex]')?.focus();
		_close();
	}

	onMount(() => {
		document.addEventListener('pointerdown', _onDocumentPointerDown, true);
		return () => document.removeEventListener('pointerdown', _onDocumentPointerDown, true);
	});
	onDestroy(_close);

	$effect(() => {
		if (!programmaticOpen) return;
		pinned = true;
		void _openNow();
	});

	$effect(() => {
		if (!open || popEl === undefined) return;
		_placeFromPop();
		const ro = new ResizeObserver(() => _placeFromPop());
		ro.observe(popEl);
		const onViewport = (): void => _placeFromPop();
		window.addEventListener('resize', onViewport);
		window.addEventListener('scroll', onViewport, true);
		return () => {
			ro.disconnect();
			window.removeEventListener('resize', onViewport);
			window.removeEventListener('scroll', onViewport, true);
		};
	});
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<span
	class="explainer"
	bind:this={wrapEl}
	onpointerenter={_show}
	onpointerleave={_hide}
	onfocusin={_show}
	onfocusout={_hide}
	onpointerdown={_pinFromClick}
	onkeydown={_onKeydown}
>
	{@render children()}
	{#if open && hasRich}
		<div
			class="pop"
			class:has-action={action !== null}
			bind:this={popEl}
			style={popStyle}
			role={action === null ? 'tooltip' : 'dialog'}
			aria-label={action === null ? undefined : title}
			onpointerenter={_show}
			onpointerleave={_hide}
		>
			<p class="head">{title}</p>
			{#if warning !== null}
				<p class="warning">{warning}</p>
			{/if}
			{#if demo === 'cue'}
				<svg class="demo" viewBox="0 0 120 36" aria-hidden="true">
					<rect x="2" y="14" width="116" height="8" rx="1" class="track" />
					<!-- memory cue marker -->
					<line x1="28" y1="8" x2="28" y2="30" class="cue-mark" />
					<!-- playhead: return to cue then stop -->
					<rect x="0" y="12" width="3" height="12" class="playhead cue-ph" />
				</svg>
			{:else if demo === 'slip'}
				<svg class="demo" viewBox="0 0 120 36" aria-hidden="true">
					<rect x="2" y="18" width="116" height="6" rx="1" class="track" />
					<!-- audible loop window -->
					<rect x="36" y="16" width="28" height="10" rx="1" class="loop-win" />
					<!-- audible playhead loops inside window -->
					<rect x="0" y="15" width="3" height="12" class="playhead slip-aud" />
					<!-- hidden linear playhead keeps advancing -->
					<rect x="0" y="4" width="3" height="8" class="playhead slip-hid" />
					<text x="4" y="11" class="label">hidden</text>
				</svg>
			{:else if demo === 'split-view'}
				<svg class="demo demo-tall" viewBox="0 0 120 44" aria-hidden="true">
					<rect x="2" y="4" width="56" height="36" rx="1" class="split-pane split-browser" />
					<rect x="62" y="4" width="56" height="36" rx="1" class="split-pane split-decks" />
					<line x1="60" y1="4" x2="60" y2="40" class="split-divider" />
					<rect x="6" y="10" width="44" height="2" class="split-row" />
					<rect x="6" y="16" width="44" height="2" class="split-row" />
					<rect x="6" y="22" width="44" height="2" class="split-row" />
					<path d="M66 28 L74 20 L82 32 L90 18" class="split-wave" fill="none" stroke-width="1.2" />
					<path d="M66 34 L78 26 L90 36" class="split-wave split-wave-2" fill="none" stroke-width="1.2" />
				</svg>
			{:else if demo === 'link'}
				<svg class="demo" viewBox="0 0 120 36" aria-hidden="true">
					<rect x="8" y="12" width="22" height="14" rx="2" class="link-deck" />
					<rect x="90" y="12" width="22" height="14" rx="2" class="link-deck" />
					<line x1="32" y1="19" x2="88" y2="19" class="link-line" />
					<circle cx="60" cy="19" r="3" class="link-beat" />
				</svg>
			{:else if demo === 'fx'}
				<svg class="demo demo-tall" viewBox="0 0 120 48" aria-hidden="true">
					<line x1="28" y1="8" x2="28" y2="40" class="fx-rail" />
					<line x1="60" y1="8" x2="60" y2="40" class="fx-rail" />
					<line x1="92" y1="8" x2="92" y2="40" class="fx-rail" />
					<rect x="22" y="28" width="12" height="10" rx="1" class="fx-bar fx-bar-a" />
					<rect x="54" y="18" width="12" height="20" rx="1" class="fx-bar fx-bar-b" />
					<rect x="86" y="24" width="12" height="14" rx="1" class="fx-bar fx-bar-a" />
				</svg>
			{:else if demo === '2-deck-view'}
				<svg class="demo demo-tall" viewBox="0 0 120 48" aria-hidden="true">
					<rect x="4" y="10" width="24" height="28" rx="1" class="two-deck-slot two-deck-grow" />
					<rect x="32" y="10" width="24" height="28" rx="1" class="two-deck-slot two-deck-grow" />
					<rect x="64" y="10" width="24" height="28" rx="1" class="two-deck-slot two-deck-hide two-deck-hide-l" />
					<rect x="92" y="10" width="24" height="28" rx="1" class="two-deck-slot two-deck-hide two-deck-hide-r" />
				</svg>
			{:else if demo === 'headphone-mix'}
				<svg class="demo demo-tall" viewBox="0 0 120 48" aria-hidden="true">
					<g class="mix-knob-cap">
						<circle cx="24" cy="24" r="10" class="mix-cap" />
						<line x1="24" y1="24" x2="24" y2="14" class="mix-pointer" />
					</g>
					<path d="M44 30 L72 30" class="hp-path cue-path" />
					<path d="M44 18 L72 18" class="hp-path master-path" />
					<circle cx="44" cy="30" r="2.5" class="hp-dot cue-dot" />
					<text x="76" y="22" class="label">M</text>
					<text x="76" y="34" class="label">C</text>
				</svg>
			{:else if demo === 'headphone-mode'}
				<svg class="demo" viewBox="0 0 120 36" aria-hidden="true">
					<g class="mode-practice">
						<rect x="12" y="10" width="28" height="16" rx="2" class="mode-speaker" />
						<path d="M26 18 v-4 a6 6 0 0 1 12 0 v4" class="mode-hp-band" fill="none" />
					</g>
					<g class="mode-split">
						<text x="58" y="14" class="label">L</text>
						<text x="58" y="28" class="label">R</text>
						<rect x="68" y="8" width="40" height="6" class="split-leg master-leg" />
						<rect x="68" y="22" width="40" height="6" class="split-leg cue-leg" />
					</g>
				</svg>
			{:else if demo === 'headphone-practice'}
				<svg class="demo" viewBox="0 0 120 36" aria-hidden="true">
					<rect x="20" y="14" width="36" height="10" rx="2" class="mode-speaker" />
					<circle cx="78" cy="19" r="4" class="cue-pulse" />
					<path d="M72 19 L68 19" class="hp-path cue-path" />
				</svg>
			{:else if demo === 'headphone-split'}
				<svg class="demo" viewBox="0 0 120 36" aria-hidden="true">
					<text x="8" y="14" class="label">L master</text>
					<text x="8" y="28" class="label">R cue</text>
					<rect x="52" y="8" width="56" height="8" class="split-leg master-leg" />
					<rect x="52" y="22" width="56" height="8" class="split-leg cue-leg" />
				</svg>
			{/if}
			{#if bullets.length > 0}
				<ul>
					{#each bullets as line (line)}
						<li>{line}</li>
					{/each}
				</ul>
			{/if}
			{#if action !== null}
				<div class="action">{@render action()}</div>
			{/if}
		</div>
	{/if}
</span>

<style>
	.explainer {
		position: relative;
		display: inline-flex;
		align-items: center;
		justify-content: center;
	}
	.pop {
		position: fixed;
		z-index: 80;
		width: max-content;
		max-width: 240px;
		padding: 7px 9px 8px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		line-height: 1.35;
		pointer-events: auto;
		text-align: left;
	}
	.pop.has-action {
		max-width: 280px;
	}
	.head {
		margin: 0 0 4px;
		font-weight: 650;
		color: var(--rb-text);
		letter-spacing: 0.02em;
	}
	.warning {
		margin: 0 0 5px;
		color: var(--rb-red);
		font-weight: 650;
	}
	.action {
		margin-top: 6px;
	}
	ul {
		margin: 0;
		padding: 0 0 0 12px;
		color: var(--rb-text-dim);
	}
	li {
		margin: 0 0 2px;
	}
	.demo {
		display: block;
		width: 100%;
		height: 36px;
		margin: 2px 0 6px;
	}
	.demo-tall {
		height: 48px;
	}
	.track {
		fill: #1a1e25;
		stroke: var(--rb-border);
		stroke-width: 1;
	}
	.cue-mark {
		stroke: var(--rb-red);
		stroke-width: 2;
		stroke-linecap: round;
	}
	.loop-win {
		fill: color-mix(in srgb, var(--rb-orange) 35%, transparent);
		stroke: var(--rb-orange);
		stroke-width: 1;
	}
	.playhead {
		fill: var(--rb-accent);
	}
	.cue-ph {
		animation: cue-return 2.4s ease-in-out infinite;
	}
	.slip-aud {
		fill: var(--rb-orange);
		animation: slip-loop 1.6s linear infinite;
	}
	.slip-hid {
		fill: var(--rb-text);
		opacity: 0.85;
		animation: slip-linear 3.2s linear infinite;
	}
	.label {
		fill: var(--rb-text-dim);
		font-size: 7px;
		font-family: var(--rb-font);
	}
	@keyframes cue-return {
		0% {
			transform: translateX(70px);
			opacity: 1;
		}
		45% {
			transform: translateX(70px);
			opacity: 1;
		}
		70% {
			transform: translateX(27px);
			opacity: 1;
		}
		85%,
		100% {
			transform: translateX(27px);
			opacity: 0.35;
		}
	}
	@keyframes slip-loop {
		0% {
			transform: translateX(36px);
		}
		100% {
			transform: translateX(61px);
		}
	}
	@keyframes slip-linear {
		0% {
			transform: translateX(36px);
		}
		100% {
			transform: translateX(108px);
		}
	}
	.split-pane {
		fill: #12161c;
		stroke: var(--rb-border);
		stroke-width: 1;
	}
	.split-browser {
		animation: split-browser-pulse 2.8s ease-in-out infinite;
	}
	.split-divider {
		stroke: var(--rb-accent);
		stroke-width: 1;
	}
	.split-row {
		fill: var(--rb-text-dim);
		opacity: 0.55;
	}
	.split-wave {
		stroke: var(--rb-orange);
		opacity: 0.85;
	}
	.split-wave-2 {
		opacity: 0.5;
	}
	.link-deck {
		fill: #141a22;
		stroke: var(--rb-border);
		stroke-width: 1;
	}
	.link-line {
		stroke: var(--rb-text-dim);
		stroke-width: 1;
		stroke-dasharray: 4 3;
	}
	.link-beat {
		fill: var(--rb-accent);
		animation: link-beat-pulse 2s ease-in-out infinite;
	}
	.mix-cap {
		fill: #1a1e25;
		stroke: var(--rb-border);
		stroke-width: 1;
	}
	.mix-pointer {
		stroke: var(--rb-text);
		stroke-width: 1.5;
		stroke-linecap: round;
	}
	.mix-knob-cap {
		transform-origin: 24px 24px;
		animation: mix-knob-turn 3s ease-in-out infinite;
		will-change: transform;
	}
	.hp-path {
		fill: none;
		stroke-width: 2;
		stroke-linecap: round;
	}
	.cue-path {
		stroke: var(--rb-orange);
	}
	.master-path {
		stroke: var(--rb-accent);
	}
	.hp-dot {
		fill: var(--rb-orange);
		animation: hp-route-dot 3s ease-in-out infinite;
		will-change: transform, opacity;
	}
	.mode-speaker {
		fill: #141a22;
		stroke: var(--rb-border);
		stroke-width: 1;
	}
	.mode-hp-band {
		stroke: var(--rb-text-dim);
		stroke-width: 1;
	}
	.mode-practice {
		animation: mode-fade-practice 4s ease-in-out infinite;
	}
	.mode-split {
		opacity: 0.35;
		animation: mode-fade-split 4s ease-in-out infinite;
	}
	.master-leg {
		fill: color-mix(in srgb, var(--rb-accent) 45%, transparent);
		stroke: var(--rb-accent);
		stroke-width: 0.8;
	}
	.cue-leg {
		fill: color-mix(in srgb, var(--rb-orange) 45%, transparent);
		stroke: var(--rb-orange);
		stroke-width: 0.8;
	}
	.cue-pulse {
		fill: var(--rb-orange);
		animation: cue-pulse 1.6s ease-in-out infinite;
	}
	@keyframes split-browser-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.72;
		}
	}
	@keyframes link-beat-pulse {
		0%,
		100% {
			opacity: 0.35;
			transform: scale(0.85);
		}
		50% {
			opacity: 1;
			transform: scale(1.15);
		}
	}
	@keyframes mix-knob-turn {
		0%,
		100% {
			transform: rotate(-45deg);
		}
		50% {
			transform: rotate(45deg);
		}
	}
	@keyframes hp-route-dot {
		0%,
		10% {
			transform: translate(0, 0);
			opacity: 1;
		}
		30% {
			transform: translate(28px, 0);
			opacity: 1;
		}
		48%,
		58% {
			transform: translate(28px, -12px);
			opacity: 1;
		}
		75% {
			transform: translate(28px, 0);
			opacity: 0.9;
		}
		100% {
			transform: translate(0, 0);
			opacity: 1;
		}
	}
	@keyframes mode-fade-practice {
		0%,
		45% {
			opacity: 1;
		}
		50%,
		95% {
			opacity: 0.25;
		}
		100% {
			opacity: 1;
		}
	}
	@keyframes mode-fade-split {
		0%,
		45% {
			opacity: 0.25;
		}
		50%,
		95% {
			opacity: 1;
		}
		100% {
			opacity: 0.25;
		}
	}
	@keyframes cue-pulse {
		0%,
		100% {
			opacity: 0.4;
		}
		50% {
			opacity: 1;
		}
	}
	.fx-rail {
		stroke: var(--rb-border);
		stroke-width: 1;
	}
	.fx-bar {
		fill: var(--rb-accent);
		transform-origin: center bottom;
		opacity: 0.55;
	}
	.fx-bar-a {
		animation: fx-send-a 1.4s ease-in-out infinite;
		will-change: transform, opacity;
	}
	.fx-bar-b {
		animation: fx-send-b 1.9s ease-in-out infinite;
		will-change: transform, opacity;
	}
	.two-deck-slot {
		fill: #141a22;
		stroke: var(--rb-border);
		stroke-width: 1;
	}
	.two-deck-grow {
		transform-origin: center center;
		animation: two-deck-grow 2.6s ease-in-out infinite;
		will-change: transform;
	}
	.two-deck-hide {
		transform-origin: center center;
		animation: two-deck-hide 2.6s ease-in-out infinite;
		will-change: transform, opacity;
	}
	.two-deck-hide-r {
		animation-delay: 0.08s;
	}
	@keyframes fx-send-a {
		0%,
		100% {
			transform: scaleY(0.45);
			opacity: 0.45;
		}
		50% {
			transform: scaleY(1);
			opacity: 1;
		}
	}
	@keyframes fx-send-b {
		0%,
		100% {
			transform: scaleY(0.35);
			opacity: 0.4;
		}
		50% {
			transform: scaleY(0.95);
			opacity: 0.95;
		}
	}
	@keyframes two-deck-hide {
		0% {
			transform: scale(1);
			opacity: 1;
		}
		35%,
		100% {
			transform: scale(0.6);
			opacity: 0;
		}
	}
	@keyframes two-deck-grow {
		0% {
			transform: scaleX(0.55);
		}
		35%,
		100% {
			transform: scaleX(1.08);
		}
	}
</style>
