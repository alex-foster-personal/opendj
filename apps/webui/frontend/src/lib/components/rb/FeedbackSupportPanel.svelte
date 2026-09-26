<script lang="ts">
	/** Compact support note panel for the global feedback dock (FB-18c, #3981). */
	import { armPinPlacement, feedbackState, queueGeneralFeedback } from '$lib/rb/feedback-store.svelte';

	type Props = {
		onclose: () => void;
	};

	let { onclose }: Props = $props();

	const unavailable = $derived(feedbackState.availability === 'missing');
	let draft = $state(feedbackState.general?.text ?? '');

	$effect(() => {
		if (feedbackState.general?.text !== undefined) {
			draft = feedbackState.general.text;
		}
	});

	function onInput(e: Event): void {
		const value = (e.currentTarget as HTMLTextAreaElement).value;
		draft = value;
		queueGeneralFeedback(value);
	}
</script>

<div class="fb-support-panel" role="dialog" aria-label="Support and feedback">
	<header class="fb-support-head">
		<span>Support</span>
		<button type="button" class="fb-support-close" aria-label="Close support panel" onclick={onclose}>
			×
		</button>
	</header>
	<p class="fb-support-hint">Questions and bugs go here. Agents harvest notes; pins stay until you archive them.</p>
	<textarea
		class="fb-support-note"
		class:rb-inert={unavailable}
		disabled={unavailable}
		placeholder={unavailable ? 'Feedback API unavailable on this daemon' : 'General note (auto-saves)'}
		title={unavailable ? 'This daemon does not serve /api/v1/feedback' : 'PUT /api/v1/feedback/general'}
		value={draft}
		oninput={onInput}
		rows="4"
	></textarea>
	<button
		type="button"
		class="fb-support-pin"
		class:rb-inert={unavailable}
		disabled={unavailable}
		onclick={() => {
			armPinPlacement();
			onclose();
		}}
	>
		Drop a comment pin on anything
	</button>
</div>

<style>
	.fb-support-panel {
		width: min(280px, calc(100vw - 24px));
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 6px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
		padding: 10px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		color: var(--text);
		font-size: 12px;
	}
	.fb-support-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		font-weight: 600;
	}
	.fb-support-close {
		border: none;
		background: transparent;
		color: var(--muted);
		cursor: pointer;
		font-size: 18px;
		line-height: 1;
		padding: 0 4px;
	}
	.fb-support-hint {
		margin: 0;
		color: var(--muted);
		line-height: 1.35;
	}
	.fb-support-note {
		width: 100%;
		resize: vertical;
		min-height: 72px;
		font: inherit;
		padding: 6px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		box-sizing: border-box;
	}
	.fb-support-pin {
		align-self: flex-start;
		font: inherit;
		padding: 6px 10px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		cursor: pointer;
		color: var(--text);
	}
	.fb-support-pin:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
</style>
