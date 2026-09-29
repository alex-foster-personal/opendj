<script lang="ts">
	/**
	 * In-app review/feedback widget (FB-01..FB-04): the topbar cluster.
	 *
	 * A down-chevron left of the vibe meter toggles the compact, draggable
	 * review-todo panel (FeedbackPanel.svelte); a comment icon arms one-shot
	 * comment-anywhere pin placement. Pin markers and placement live in
	 * FeedbackPinLayer.svelte at the app root (FB-16).
	 */
	import { onMount } from 'svelte';
	import { describePinStatusSummary } from '$lib/rb/feedback';
	import { onPinsVisibleChanged, readPinsVisible, writePinsVisible } from '$lib/rb/feedback-pin-visibility';
	import { setShowAgentPins, uiPrefs } from '$lib/rb/prefs.svelte';
	import {
		armPinPlacement,
		feedbackState,
		toggleFeedbackPanel
	} from '$lib/rb/feedback-store.svelte';
	import ControlExplainer from './deck/ControlExplainer.svelte';
	import FeedbackPanel from './FeedbackPanel.svelte';
	import FeedbackPinVisibilityActions from './FeedbackPinVisibilityActions.svelte';

	const FEEDBACK_UNAVAILABLE =
		'Review todos - this daemon does not serve /api/v1/feedback, so in-app review is unavailable';
	const COMMENT_UNAVAILABLE =
		'Comment pins - this daemon does not serve /api/v1/feedback, so dropping a pin is unavailable';

	const FEEDBACK_EXPLAINER_TITLE =
		'Give feedback, ideas and suggestions to the developer, and track them in-app.';

	const openCount = $derived(feedbackState.todos.filter((t) => !t.done).length);

	const unavailable = $derived(feedbackState.availability === 'missing');
	const commentPinTitle = $derived.by(() => {
		if (unavailable) return COMMENT_UNAVAILABLE;
		if (feedbackState.availability === 'unknown')
			return 'Comment pins - probing the daemon for /api/v1/feedback';
		const summary = describePinStatusSummary(feedbackState.pins);
		return feedbackState.placementArmed
			? `${summary}. Click anywhere to drop a comment pin (Esc cancels)`
			: `${summary}. Click to drop a comment pin anywhere on the UI`;
	});

	const commentPinBullets = $derived.by(() => {
		if (unavailable) return [COMMENT_UNAVAILABLE];
		if (feedbackState.availability === 'unknown') {
			return ['Probing the daemon for /api/v1/feedback'];
		}
		return [
			describePinStatusSummary(feedbackState.pins),
			'Press M to arm comment placement (or Cmd+Shift+M from a text field).',
			'Delegated / in-progress / queued are not tracked by the comment API yet.'
		];
	});
	const chevronTitle = $derived.by(() => {
		if (unavailable) return FEEDBACK_UNAVAILABLE;
		if (feedbackState.availability === 'unknown')
			return 'Review todos - probing the daemon for /api/v1/feedback (click retries)';
		return `Review todos - ${openCount} open item(s) agents queued for the maintainer's review; check done, pick options, type feedback (auto-saves)`;
	});

	let pinsVisible = $state(false);

	/* Other surfaces also write this preference (the `M` hotkey reveal in
	 * FeedbackPinLayer, the agent-facing `__mdtPinsVisible` twin), so the
	 * checkbox re-reads on every change rather than trusting its mount read. */
	function syncPinsVisible(): void {
		pinsVisible = readPinsVisible(window.localStorage);
	}

	onMount(() => {
		syncPinsVisible();
		const offPinsVisibleChanged = onPinsVisibleChanged(syncPinsVisible);
		return () => {
			offPinsVisibleChanged();
		};
	});

	function togglePinsVisible(): void {
		pinsVisible = !pinsVisible;
		writePinsVisible(window.localStorage, pinsVisible);
	}

	function toggleAgentPins(): void {
		setShowAgentPins(!uiPrefs.show_agent_pins);
	}
</script>

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
	{#snippet pinVisibilityActions()}
		<FeedbackPinVisibilityActions
			{pinsVisible}
			showAgentPins={uiPrefs.show_agent_pins}
			ontoggle={togglePinsVisible}
			onToggleAgentPins={toggleAgentPins}
		/>
	{/snippet}
	<ControlExplainer
		title={FEEDBACK_EXPLAINER_TITLE}
		bullets={commentPinBullets}
		action={pinVisibilityActions}
		placement="right"
	>
		<button
			type="button"
			class="fb-btn fb-place-skip"
			class:rb-inert={unavailable}
			class:armed={feedbackState.placementArmed}
			disabled={unavailable}
			title={commentPinTitle}
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
	</ControlExplainer>
</span>

<FeedbackPanel />

<style>
	.fb-cluster {
		position: absolute;
		right: calc(50% + 92px);
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
</style>
