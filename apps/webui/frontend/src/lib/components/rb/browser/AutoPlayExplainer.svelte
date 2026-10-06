<script lang="ts">
	// Interactive hover/focus panel teaching what the AutoPlay column means
	// and why a row holds its rank. Modeled on TopBar's ap-wrap/ap-menu
	// interactive floating panel (not ControlExplainer's inert, pointer-events
	// none tooltip) because it must host a real, clickable Warnsdorff link.
	// Opens to the LEFT of its trigger. Column explainers default upward so they
	// do not obscure the table below; a caller may deliberately opt below.
	import { onDestroy } from 'svelte';
	import type { Snippet } from 'svelte';
	import type { AutoPlayQueueEntry } from '$lib/rb/auto-play';
	import { describeAutoPlayMode } from '$lib/rb/autoplay-mode';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import {
		columnExplainerStyle,
		type ColumnExplainerPlacement
	} from './column-explainer-placement';

	const WARNSDORFF_LABEL = "Warnsdorff's rule";
	const WARNSDORFF_HREF = 'https://en.wikipedia.org/wiki/Warnsdorff%27s_rule';
	const HIDE_DELAY_MS = 150;

	let {
		demo,
		queue = [],
		placement = 'above',
		children
	}: {
		/** Reserved mount point for the mini-library walkthrough animation. */
		demo?: Snippet;
		/** Frozen handoff rows, preserved independently of the live browser filter. */
		queue?: readonly AutoPlayQueueEntry[];
		/** Column explainers normally clear the table; below is opt-in. */
		placement?: ColumnExplainerPlacement;
		/** The robot header trigger this panel opens from. */
		children: Snippet;
	} = $props();

	let wrapEl: HTMLSpanElement | undefined = $state();
	let panelEl: HTMLDivElement | undefined = $state();
	let open = $state(false);
	let panelStyle = $state('');
	let hideTimer: ReturnType<typeof setTimeout> | undefined;

	const mode = $derived(describeAutoPlayMode(uiPrefs));

	function _place(): void {
		if (!wrapEl || !panelEl) return;
		panelStyle = columnExplainerStyle(
			wrapEl.getBoundingClientRect(),
			panelEl.getBoundingClientRect(),
			window,
			placement
		);
	}

	function _cancelHide(): void {
		if (hideTimer === undefined) return;
		clearTimeout(hideTimer);
		hideTimer = undefined;
	}

	function _close(): void {
		_cancelHide();
		open = false;
	}

	/**
	 * Keep the interactive panel out of the sticky table-header stacking context.
	 * This is deliberately a DOM portal rather than the Popover API: the shipped
	 * macOS 11 WKWebView predates HTMLElement.showPopover().
	 */
	function _portalToBody(node: HTMLElement): { destroy: () => void } {
		document.body.appendChild(node);
		return {
			destroy: () => node.remove()
		};
	}

	function _show(): void {
		_cancelHide();
		open = true;
	}

	function _hide(e: FocusEvent | PointerEvent): void {
		const next = e instanceof FocusEvent ? e.relatedTarget : (e as PointerEvent).relatedTarget;
		if (next instanceof Node && wrapEl?.contains(next)) return;
		// The panel is portalled to document.body, outside the wrapper's DOM branch.
		if (next instanceof Element && next.closest?.('.ap-explain-panel')) return;
		if (e instanceof PointerEvent) {
			_cancelHide();
			hideTimer = setTimeout(_close, HIDE_DELAY_MS);
			return;
		}
		_close();
	}

	$effect(() => {
		if (!open || !panelEl) return;
		_place();
		const reposition = () => _place();
		window.addEventListener('resize', reposition);
		window.addEventListener('scroll', reposition, true);
		return () => {
			window.removeEventListener('resize', reposition);
			window.removeEventListener('scroll', reposition, true);
		};
	});

	onDestroy(_cancelHide);
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<span
	class="ap-explain-wrap"
	data-custom-tip=""
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
			bind:this={panelEl}
			use:_portalToBody
			style={panelStyle}
			role="dialog"
			tabindex="-1"
			aria-label="AutoPlay order"
			onpointerenter={_show}
			onpointerleave={_hide}
		>
			<p class="ap-explain-head">AutoPlay order</p>
			<p class="ap-explain-mode">{mode.short} - {mode.detail}</p>
			<p class="ap-explain-sub">Published queue:</p>
			{#if queue.length === 0}
				<p class="ap-explain-queue-empty">No handoffs planned yet.</p>
			{:else}
				<ol class="ap-explain-queue" aria-label="Published AutoPlay queue">
					{#each queue as entry, index (entry.stable_id)}
						<li>
							<strong>{index + 1}. {entry.title}</strong>
							{entry.artist === null ? '' : ` - ${entry.artist}`}
						</li>
					{/each}
				</ol>
			{/if}

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
		inset: auto;
		margin: 0;
		width: min(300px, calc(100vw - 16px));
		max-height: calc(100vh - 16px);
		overflow-y: auto;
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
	.ap-explain-queue,
	.ap-explain-queue-empty {
		margin: 0;
	}
	.ap-explain-queue {
		padding: 0 0 0 18px;
	}
	.ap-explain-queue li {
		margin: 0 0 2px;
	}
	.ap-explain-queue-empty {
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
