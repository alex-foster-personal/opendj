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
	import { onDestroy } from 'svelte';
	import type { Snippet } from 'svelte';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';

	const INTERACTIVE_HIDE_DELAY_MS = 150;

	export type ExplainerDemo = 'cue' | 'slip';

	let {
		title,
		bullets = [],
		warning = null,
		action = null,
		demo = null,
		placement = 'auto',
		showDelayMs = 0,
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
		/** Prefer above; `auto` flips below when near the top of the viewport. */
		placement?: 'auto' | 'above' | 'below';
		/**
		 * Trailing show debounce in ms. Default 0 = show immediately, which
		 * preserves every existing caller's current behavior. A caller with a
		 * reason to debounce (e.g. several of these packed edge to edge, where
		 * a fast skim across them would otherwise flash a popover per item)
		 * sets its own value and states that reason at the call site.
		 */
		showDelayMs?: number;
		children: Snippet;
	} = $props();

	let wrapEl: HTMLSpanElement | undefined = $state();
	let open = $state(false);
	let popStyle = $state('');
	let hideTimer: ReturnType<typeof setTimeout> | undefined;
	let showTimer: ReturnType<typeof setTimeout> | undefined;

	const hasRich: boolean = $derived(bullets.length > 0 || warning !== null || action !== null || demo !== null);

	function _place(): void {
		if (!wrapEl) return;
		const r = wrapEl.getBoundingClientRect();
		const preferAbove =
			placement === 'above' || (placement === 'auto' && r.top > 170);
		const size = { width: 240, height: 120 };
		const proposedX = r.left + r.width / 2 - size.width / 2;
		const proposedY = preferAbove ? r.top - size.height - 6 : r.bottom + 6;
		const box = clampToViewport(
			proposedX,
			proposedY,
			size,
			{ width: window.innerWidth, height: window.innerHeight }
		);
		popStyle = `left:${Math.round(box.x)}px;top:${Math.round(box.y)}px`;
	}

	function _openNow(): void {
		showTimer = undefined;
		_place();
		open = true;
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
		open = false;
	}

	function _hide(event: FocusEvent | PointerEvent): void {
		const next = event.relatedTarget;
		if (next instanceof Node && wrapEl?.contains(next)) return;
		if (event instanceof PointerEvent && action !== null) {
			if (hideTimer !== undefined) clearTimeout(hideTimer);
			hideTimer = setTimeout(_close, INTERACTIVE_HIDE_DELAY_MS);
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

	onDestroy(_close);
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<span
	class="explainer"
	bind:this={wrapEl}
	onpointerenter={_show}
	onpointerleave={_hide}
	onfocusin={_show}
	onfocusout={_hide}
	onkeydown={_onKeydown}
>
	{@render children()}
	{#if open && hasRich}
		<div
			class="pop"
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
</style>
