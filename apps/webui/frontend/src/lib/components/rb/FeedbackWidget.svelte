<script lang="ts">
	/**
	 * In-app review/feedback widget (FB-01..FB-04): the topbar cluster.
	 *
	 * A down-chevron beside the vibe meter toggles the compact, draggable
	 * review-todo panel (FeedbackPanel.svelte); a comment icon arms one-shot
	 * comment-anywhere pin placement. Everything the user does here lands on
	 * /api/v1/feedback immediately (debounced for text) - the same endpoints
	 * agents use, so there is nothing UI-only to lose.
	 *
	 * Honest rendering: on a daemon with no /api/v1/feedback the chevron and
	 * comment icon render inert with the standard PARITY-TODO tooltip, never
	 * a broken panel.
	 */
	import { onMount } from 'svelte';
	import { describeAnchor, pinFromClient, pinStyle, type PinPoint } from '$lib/rb/feedback';
	import {
		addPin,
		armPinPlacement,
		disarmPinPlacement,
		feedbackState,
		flushFeedbackSaves,
		hydrateFeedback,
		toggleFeedbackPanel
	} from '$lib/rb/feedback-store.svelte';
	import FeedbackPanel from './FeedbackPanel.svelte';

	const INERT_TITLE = 'not implemented - see PARITY-TODO';

	/** A placed-but-unsaved pin: the bubble the user is typing into. */
	let pinDraft: { point: PinPoint; anchor: string | null; text: string } | null =
		$state(null);

	let pathname = $state('/');

	const openCount = $derived(feedbackState.todos.filter((t) => !t.done).length);
	const pagePins = $derived(feedbackState.pins.filter((p) => p.page === pathname));

	const unavailable = $derived(feedbackState.availability === 'missing');
	const chevronTitle = $derived.by(() => {
		if (unavailable) return INERT_TITLE;
		if (feedbackState.availability === 'unknown')
			return 'Review todos - probing the daemon for /api/v1/feedback (click retries)';
		return `Review todos - ${openCount} open item(s) agents queued for the maintainer's review; check done, pick options, type feedback (auto-saves)`;
	});

	onMount(() => {
		pathname = window.location.pathname;
		void hydrateFeedback();
		const flush = () => flushFeedbackSaves();
		window.addEventListener('pagehide', flush);
		return () => {
			flushFeedbackSaves();
			window.removeEventListener('pagehide', flush);
		};
	});

	// ----- pin placement --------------------------------------------------
	function handlePlacementClick(e: MouseEvent): void {
		const point = pinFromClient(
			e.clientX,
			e.clientY,
			window.innerWidth,
			window.innerHeight
		);
		// The overlay button is the top hit; the anchor is what is under it.
		const under = document
			.elementsFromPoint(e.clientX, e.clientY)
			.find((el) => !el.classList.contains('fb-place-overlay'));
		disarmPinPlacement();
		pinDraft = {
			point,
			anchor: describeAnchor((under as unknown as Parameters<typeof describeAnchor>[0]) ?? null),
			text: ''
		};
	}

	async function savePinDraft(): Promise<void> {
		if (pinDraft === null || pinDraft.text.trim() === '') return;
		const saved = await addPin({
			x_pct: pinDraft.point.x_pct,
			y_pct: pinDraft.point.y_pct,
			anchor: pinDraft.anchor,
			page: pathname,
			text: pinDraft.text.trim()
		});
		if (saved) pinDraft = null;
	}

	function handleEscape(e: KeyboardEvent): void {
		if (e.key !== 'Escape') return;
		if (feedbackState.placementArmed) disarmPinPlacement();
		else if (pinDraft !== null) pinDraft = null;
	}
</script>

<svelte:window onkeydown={handleEscape} />

<span class="fb-cluster">
	<button
		type="button"
		class="fb-btn"
		class:rb-inert={unavailable}
		class:open={feedbackState.panelOpen}
		disabled={unavailable}
		title={chevronTitle}
		aria-label="Review todos panel"
		aria-expanded={feedbackState.panelOpen}
		onclick={toggleFeedbackPanel}
	>
		<svg width="9" height="6" viewBox="0 0 9 6" aria-hidden="true">
			<path d="M1 1.2 L4.5 4.8 L8 1.2" fill="none" stroke="currentColor" stroke-width="1.4" />
		</svg>
		{#if feedbackState.availability === 'ok' && openCount > 0}
			<span
				class="fb-count"
				title={`${openCount} review todo(s) not yet marked done`}>{openCount}</span
			>
		{/if}
	</button>
	<button
		type="button"
		class="fb-btn"
		class:rb-inert={unavailable}
		class:armed={feedbackState.placementArmed}
		disabled={unavailable}
		title={unavailable
			? INERT_TITLE
			: feedbackState.placementArmed
				? 'Click anywhere to drop a comment pin (Esc cancels)'
				: 'Drop a comment anywhere on the UI - arms one placement click'}
		aria-label="Drop a comment pin"
		aria-pressed={feedbackState.placementArmed}
		onclick={armPinPlacement}
	>
		<svg width="11" height="10" viewBox="0 0 12 11" aria-hidden="true">
			<path
				d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z"
				fill="none"
				stroke="currentColor"
				stroke-width="1.2"
				stroke-linejoin="round"
			/>
		</svg>
	</button>
</span>

<!-- comment pins on this page: markers only, tooltip carries the content -->
{#each pagePins as pin (pin.id)}
	<span
		class="fb-pin"
		style={pinStyle(pin)}
		title={`${pin.text} - ${pin.created_at}${pin.anchor ? ` (near ${pin.anchor})` : ''}`}
	>
		<svg width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
			<path
				d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z"
				fill="currentColor"
				stroke="none"
			/>
		</svg>
	</span>
{/each}

<!-- one-shot placement mode: a full-viewport button so the next click is the pin -->
{#if feedbackState.placementArmed}
	<button
		type="button"
		class="fb-place-overlay"
		aria-label="Click to place the comment pin, Escape to cancel"
		onclick={handlePlacementClick}
	></button>
{/if}

<!-- pin text bubble -->
{#if pinDraft !== null}
	<div class="fb-bubble" style={pinStyle(pinDraft.point)} role="dialog" aria-label="New comment pin">
		<textarea
			class="fb-bubble-text"
			rows="3"
			placeholder="What is wrong / right here?"
			bind:value={pinDraft.text}
		></textarea>
		{#if pinDraft.anchor !== null}
			<p class="fb-hint" title="Best-effort nearest stable element under the click">near {pinDraft.anchor}</p>
		{/if}
		<div class="fb-row-btns">
			<button type="button" class="fb-mini" onclick={savePinDraft} disabled={pinDraft.text.trim() === ''}>
				Save pin
			</button>
			<button type="button" class="fb-mini" onclick={() => (pinDraft = null)}>Cancel</button>
		</div>
	</div>
{/if}

<FeedbackPanel />

<style>
	.fb-cluster {
		position: absolute;
		left: calc(50% + 92px);
		top: 50%;
		transform: translateY(-50%);
		display: inline-flex;
		align-items: center;
		gap: 3px;
		z-index: 2;
	}

	.fb-btn {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		height: 18px;
		padding: 0 4px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		cursor: pointer;
		line-height: 1;
	}
	.fb-btn:hover:not(:disabled) {
		color: var(--rb-text);
	}
	.fb-btn.open,
	.fb-btn.armed {
		color: var(--rb-accent);
		border-color: color-mix(in srgb, var(--rb-accent) 55%, var(--rb-border));
	}
	.fb-btn.open svg {
		transform: rotate(180deg);
	}

	.fb-count {
		font-family: var(--rb-font);
		font-size: 9px;
		font-weight: 700;
		color: var(--rb-orange);
	}

	.fb-pin {
		position: fixed;
		z-index: 80;
		transform: translate(-50%, -50%);
		color: var(--rb-orange);
		cursor: help;
	}

	.fb-place-overlay {
		position: fixed;
		inset: 0;
		z-index: 300;
		background: transparent;
		border: none;
		cursor: crosshair;
		padding: 0;
	}

	.fb-bubble {
		position: fixed;
		z-index: 310;
		width: 200px;
		padding: 6px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
	}

	.fb-bubble-text {
		width: 100%;
		box-sizing: border-box;
		background: var(--rb-inset);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		padding: 3px 4px;
	}

	.fb-row-btns {
		display: flex;
		gap: 4px;
		margin-top: 4px;
	}

	.fb-mini {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 9px;
		padding: 2px 6px;
		line-height: 1.2;
		cursor: pointer;
	}
	.fb-mini:hover:not(:disabled) {
		color: var(--rb-text);
	}

	.fb-hint {
		margin: 2px 0 0;
		color: var(--rb-text-dim);
	}
</style>
