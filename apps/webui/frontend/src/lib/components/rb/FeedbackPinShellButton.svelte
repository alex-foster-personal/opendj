<script lang="ts" module>
	/* A plain counter, not $props.id(): this client-rendered app needs no
	 * hydration-stable id, and $props.id() pulls its SSR-id helper into the
	 * eager "/" runtime chunk (PR #4094, library bundle budget). */
	let nextSummaryId = 0;
</script>

<script lang="ts">
	/** Compact comment-pin affordance for the app-shell topbar (FB-16) and,
	 * with `overlay`, the always-on bottom-right dock. The FB-20 total +
	 * breakdown reaches hover (native title), keyboard focus (ControlExplainer,
	 * as on the /performance widget) and screen readers (aria-describedby),
	 * not the mouse alone. Visibility checkboxes match the top comment control
	 * and write the same preference. */
	import { onMount } from 'svelte';
	import {
		armPinPlacement,
		commentPinSummaryBullets,
		commentPinSummaryTitle,
		endCommentPinPeek,
		feedbackState,
		setCommentPinPeek
	} from '$lib/rb/feedback-store.svelte';
	import { onPinsVisibleChanged, readPinsVisible, writePinsVisible } from '$lib/rb/feedback-pin-visibility';
	import ControlExplainer from './deck/ControlExplainer.svelte';
	import FeedbackPinVisibilityActions from './FeedbackPinVisibilityActions.svelte';

	let { overlay = false }: { overlay?: boolean } = $props();

	const COMMENT_UNAVAILABLE =
		'Comment pins - this daemon does not serve /api/v1/feedback, so dropping a pin is unavailable';
	const EXPLAINER_TITLE =
		'Give feedback, ideas and suggestions to the developer, and track them in-app.';
	const summaryId = `fb-shell-pin-summary-${++nextSummaryId}`;
	function peekSource(): string {
		return overlay ? 'dock-comment' : 'shell-comment';
	}
	let pinsVisible = $state(false);

	function syncPinsVisible(): void {
		pinsVisible = readPinsVisible(window.localStorage);
	}

	onMount(() => {
		syncPinsVisible();
		const off = onPinsVisibleChanged(syncPinsVisible);
		return () => {
			off();
			setCommentPinPeek(peekSource(), false);
		};
	});

	function togglePinsVisible(): void {
		pinsVisible = !pinsVisible;
		writePinsVisible(window.localStorage, pinsVisible);
	}


	const unavailable = $derived(feedbackState.availability === 'missing');
	const commentPinTitle = $derived.by(() => {
		if (unavailable) return COMMENT_UNAVAILABLE;
		if (feedbackState.availability === 'unknown')
			return 'Comment pins - probing the daemon for /api/v1/feedback';
		const summary = commentPinSummaryTitle(feedbackState.pins);
		return feedbackState.placementArmed
			? `${summary}. Click anywhere to drop a comment pin (Esc cancels)`
			: `${summary}. Click to drop a comment pin anywhere on the UI`;
	});
	const commentPinBullets = $derived.by(() => {
		if (unavailable) return [COMMENT_UNAVAILABLE];
		if (feedbackState.availability === 'unknown') {
			return ['Probing the daemon for /api/v1/feedback'];
		}
		return commentPinSummaryBullets(feedbackState.pins);
	});
</script>

<span
	class="fb-pin-peek"
	role="group"
	onpointerenter={() => setCommentPinPeek(peekSource(), true)}
	onpointerleave={() => setCommentPinPeek(peekSource(), false)}
	onfocusin={() => setCommentPinPeek(peekSource(), true)}
	onfocusout={(event) => endCommentPinPeek(peekSource(), event)}
>
	{#snippet pinVisibilityActions()}
		<FeedbackPinVisibilityActions
			{pinsVisible}
			ontoggle={togglePinsVisible}
		/>
	{/snippet}
	<ControlExplainer
		title={EXPLAINER_TITLE}
		bullets={commentPinBullets}
		action={pinVisibilityActions}
		placement={overlay ? 'above' : 'below'}
	>
		<button
			type="button"
			class="fb-shell-pin fb-place-skip"
			class:overlay
			class:rb-inert={unavailable}
			class:armed={feedbackState.placementArmed}
			disabled={unavailable}
			title={commentPinTitle}
			aria-label="Drop a comment pin"
			aria-describedby={summaryId}
			aria-pressed={feedbackState.placementArmed}
			onclick={armPinPlacement}
		>
			<svg width={overlay ? 16 : 11} height={overlay ? 14 : 10} viewBox="0 0 12 11" aria-hidden="true">
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
	<span id={summaryId} class="fb-sr-only">{commentPinTitle}</span>
</span>

<style>
	.fb-sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
		border: 0;
	}
	.fb-shell-pin {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		height: 28px;
		width: 28px;
		padding: 0;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 4px;
		color: var(--muted);
		cursor: pointer;
		line-height: 0;
	}
	.fb-shell-pin:hover:not(:disabled) {
		color: var(--accent);
		border-color: var(--accent);
	}
	.fb-shell-pin.armed {
		color: var(--accent);
		border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
	}
	/* Bottom-right overlay only: a circle with a fixed yellow outline so it
	   stays the same on every page, including the light theme. */
	.fb-shell-pin.overlay,
	.fb-shell-pin.overlay:hover:not(:disabled),
	.fb-shell-pin.overlay.armed {
		width: 36px;
		height: 36px;
		border-radius: 50%;
		border: 2px solid #ffb43a;
		color: #ffb43a;
		/* Fixed dark face to match the help bubble beside it (FeedbackDock). */
		background: #121720;
		box-shadow: 0 2px 6px rgb(0 0 0 / 35%);
	}
	.fb-shell-pin.overlay.armed {
		background: color-mix(in srgb, #ffb43a 22%, #121720);
	}
	.fb-pin-peek {
		position: relative;
		display: inline-flex;
		align-items: center;
	}
</style>
