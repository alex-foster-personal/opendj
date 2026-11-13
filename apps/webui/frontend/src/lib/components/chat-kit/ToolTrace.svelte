<script lang="ts">
	import { toolTraceStepTitle, type ToolStepRef } from '$lib/chat-kit/types';

	interface Props {
		steps?: ToolStepRef[];
		streaming?: boolean;
	}

	const { steps = [], streaming = false }: Props = $props();
</script>

<ol
	class="chat-tool-trace"
	class:streaming
	data-testid="chat-tool-trace"
	data-streaming={streaming ? 'true' : 'false'}
	aria-label="Tool steps"
	aria-busy={streaming ? 'true' : undefined}
>
	{#each steps as step (step.id)}
		<li
			class="chat-tool-step"
			class:running={step.status === 'running'}
			class:ok={step.status === 'ok'}
			class:failed={step.status === 'failed'}
			data-step-id={step.id}
			data-status={step.status}
			title={toolTraceStepTitle(step.status)}
		>
			<span class="marker" aria-hidden="true"></span>
			<span class="name">{step.name}</span>
			<span class="status">{toolTraceStepTitle(step.status)}</span>
		</li>
	{/each}
</ol>

<style>
	.chat-tool-trace {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		font-size: 0.78rem;
		line-height: 1.35;
	}
	.chat-tool-step {
		display: grid;
		grid-template-columns: auto 1fr auto;
		align-items: center;
		gap: 0.45rem;
		padding: 0.3rem 0.45rem;
		border-radius: 6px;
		border: 1px solid var(--chat-tool-border, var(--border));
		background: var(--chat-tool-bg, var(--surface-raised));
		color: var(--fg);
	}
	.marker {
		width: 0.45rem;
		height: 0.45rem;
		border-radius: 50%;
		background: currentColor;
		flex-shrink: 0;
	}
	.name {
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.status {
		font-size: 0.72rem;
		color: var(--muted);
	}
	.running {
		color: var(--chat-tool-running-fg, var(--accent));
		border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
	}
	.running .marker {
		animation: chat-tool-pulse 1.2s ease-in-out infinite;
	}
	.ok {
		color: var(--chat-tool-ok-fg, var(--success));
		border-color: color-mix(in srgb, var(--success) 45%, var(--border));
	}
	.failed {
		color: var(--chat-tool-failed-fg, var(--danger));
		border-color: color-mix(in srgb, var(--danger) 45%, var(--border));
	}
	@keyframes chat-tool-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.35;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.running .marker {
			animation: none;
		}
	}
</style>
