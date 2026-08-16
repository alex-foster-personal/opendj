<script lang="ts">
	// Interactive hover/focus panel teaching what the AutoPlay column means
	// and why a row holds its rank. Modeled on TopBar's ap-wrap/ap-menu
	// interactive floating panel (not ControlExplainer's inert, pointer-events
	// none tooltip) because it must host a real, clickable Warnsdorff link.
	// Opens to the LEFT of its trigger, top-anchored, with a downward nudge
	// only when that would clip the top of the viewport.
	import type { Snippet } from 'svelte';
	import { describeAutoPlayMode } from '$lib/rb/autoplay-mode';
	import { uiPrefs } from '$lib/rb/prefs.svelte';

	const WARNSDORFF_LABEL = "Warnsdorff's rule";
	const WARNSDORFF_HREF = 'https://en.wikipedia.org/wiki/Warnsdorff%27s_rule';
	const PANEL_GAP_PX = 6;
	const VIEWPORT_TOP_MARGIN_PX = 8;

	let {
		demo,
		children
	}: {
		/** Reserved mount point for the mini-library walkthrough animation. */
		demo?: Snippet;
		/** The robot header trigger this panel opens from. */
		children: Snippet;
	} = $props();

	let wrapEl: HTMLSpanElement | undefined = $state();
	let open = $state(false);
	let panelStyle = $state('');

	const mode = $derived(describeAutoPlayMode(uiPrefs));

	function _place(): void {
		if (wrapEl === undefined) return;
		const r = wrapEl.getBoundingClientRect();
		const top = Math.max(VIEWPORT_TOP_MARGIN_PX, Math.round(r.top));
		const right = Math.round(window.innerWidth - r.left + PANEL_GAP_PX);
		panelStyle = `right:${right}px;top:${top}px;`;
	}

	function _show(): void {
		_place();
		open = true;
	}

	function _hide(e: FocusEvent | PointerEvent): void {
		const next = e instanceof FocusEvent ? e.relatedTarget : (e as PointerEvent).relatedTarget;
		if (next instanceof Node && wrapEl?.contains(next)) return;
		// Fixed panel is outside the wrap - keep open when moving into it.
		if (next instanceof Element && next.closest?.('.ap-explain-panel')) return;
		open = false;
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<span
	class="ap-explain-wrap"
	bind:this={wrapEl}
	onpointerenter={_show}
	onpointerleave={_hide}
	onfocusin={_show}
	onfocusout={_hide}
>
	{@render children()}
	{#if open}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="ap-explain-panel"
			style={panelStyle}
			role="dialog"
			tabindex="-1"
			aria-label="AutoPlay order"
			onpointerenter={_show}
			onpointerleave={() => (open = false)}
		>
			<p class="ap-explain-head">AutoPlay order</p>
			<p class="ap-explain-mode">{mode.short} - {mode.detail}</p>

			<p class="ap-explain-sub">How AutoPlay ranks the next tracks in this playlist:</p>
			<ul class="ap-explain-bullets">
				<li>
					<strong>Greedy:</strong> loads the earliest track in playlist order that clears the rules
					below.
				</li>
				<li>
					<strong>Maximize reach (default on):</strong> loads the compatible track with the fewest
					onward options first, so tracks that are harder to follow get used before they become
					unreachable dead ends. This is
					<a href={WARNSDORFF_HREF} target="_blank" rel="noopener noreferrer"
						>{WARNSDORFF_LABEL}</a
					>, the same fewest-onward heuristic used for chess knight's tours.
				</li>
				<li>
					<strong>Enforce order:</strong> ignores the rules below entirely and just walks the playlist's
					own order, skipping only rows that are missing or already handled.
				</li>
			</ul>

			<p class="ap-explain-sub">
				A track must clear all of these to be picked (except in Enforce order):
			</p>
			<ul class="ap-explain-bullets">
				<li>Camelot key within 1 step of the currently playing track.</li>
				<li>BPM inside the current Beat Sync pitch window.</li>
				<li>Present on disk right now - a missing file is always skipped.</li>
			</ul>

			<p class="ap-explain-hint">
				If a picked track fails to load or play, AutoPlay quarantines it out of the running order
				and re-arms it (up to 3 tries) only if the playlist itself changes - so one bad handoff
				cannot permanently stall the queue.
			</p>

			<div class="ap-explain-demo-slot">
				{#if demo}
					{@render demo()}
				{/if}
			</div>
		</div>
	{/if}
</span>

<style>
	.ap-explain-wrap {
		position: relative;
		display: inline-flex;
		align-items: center;
	}
	.ap-explain-panel {
		position: fixed;
		z-index: 90;
		width: 300px;
		padding: 8px 9px 9px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		line-height: 1.4;
		text-align: left;
	}
	.ap-explain-head {
		margin: 0 0 2px;
		font-weight: 650;
		letter-spacing: 0.02em;
	}
	.ap-explain-mode {
		margin: 0 0 6px;
		color: var(--rb-accent);
	}
	.ap-explain-sub {
		margin: 6px 0 3px;
		color: var(--rb-text-dim);
	}
	.ap-explain-bullets {
		margin: 0;
		padding: 0 0 0 12px;
	}
	.ap-explain-bullets li {
		margin: 0 0 3px;
	}
	.ap-explain-bullets a {
		color: var(--rb-accent);
	}
	.ap-explain-hint {
		margin: 6px 0 0;
		color: var(--rb-text-dim);
	}
	.ap-explain-demo-slot {
		margin-top: 8px;
		min-height: 64px;
	}
</style>
