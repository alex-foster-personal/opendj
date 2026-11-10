<script lang="ts">
	import { appendTextDelta, type StreamingBubbleRole } from '$lib/chat-kit/types';

	interface Props {
		/** Assistant text settled so far. */
		text?: string;
		/** One partial token delta on top of `text` for live streaming. */
		delta?: string;
		streaming?: boolean;
		role?: StreamingBubbleRole;
	}

	const { text = '', delta = '', streaming = false, role = 'assistant' }: Props = $props();
	const visible = $derived(appendTextDelta(text, delta));
</script>

<div
	class="chat-streaming-bubble"
	class:streaming
	data-role={role}
	data-streaming={streaming ? 'true' : 'false'}
	aria-live={streaming ? 'polite' : 'off'}
>
	<p>
		{visible}{#if streaming}<span class="cursor" aria-hidden="true"></span>{/if}
	</p>
</div>

<style>
	.chat-streaming-bubble {
		max-width: 32rem;
		padding: 0.55rem 0.7rem;
		border-radius: 8px;
		border: 1px solid var(--chat-bubble-border, var(--border));
		background: var(--chat-bubble-bg, var(--surface-raised));
		color: var(--fg);
		font-size: 0.88rem;
		line-height: 1.45;
	}
	.chat-streaming-bubble p {
		margin: 0;
		white-space: pre-wrap;
		word-break: break-word;
	}
	.cursor {
		display: inline-block;
		width: 0.45em;
		height: 1em;
		margin-left: 1px;
		vertical-align: -2px;
		background: var(--chat-bubble-cursor, var(--accent));
		animation: chat-cursor-blink 1s steps(1) infinite;
	}
	@keyframes chat-cursor-blink {
		0%,
		49% {
			opacity: 1;
		}
		50%,
		100% {
			opacity: 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.cursor {
			animation: none;
			opacity: 1;
		}
	}
</style>
