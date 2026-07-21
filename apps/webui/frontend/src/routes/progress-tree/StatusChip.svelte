<script lang="ts">
	/** Colour-coded status chip. verified gets a check glyph; building pulses. */
	import { STATUS_CLASS, type NodeStatus } from './types';

	let { status }: { status: NodeStatus } = $props();
</script>

<span class="status-chip {STATUS_CLASS[status]}">
	{status}{#if status === 'verified'}<span class="check">&#10003;</span>{/if}
</span>

<style>
	.status-chip {
		display: inline-block;
		min-width: 4.6rem;
		text-align: center;
		padding: 0.05rem 0.5rem;
		border-radius: 999px;
		font-size: 0.72rem;
		font-weight: 600;
		border: 1px solid transparent;
	}
	.check {
		margin-left: 0.25rem;
	}
	.st-missing {
		color: #9aa4b2;
		border-color: #3a4250;
		background: #1a212c;
	}
	.st-spiked {
		color: #c4b5fd;
		border-color: #6d5bb8;
		background: #241d3a;
	}
	.st-building {
		color: #ffb43a;
		border-color: #7a5a1f;
		background: #2a2110;
		animation: pulse 1.6s ease-in-out infinite;
	}
	.st-partial {
		color: #7cc0ff;
		border-color: #2c5d8f;
		background: #12233a;
	}
	.st-working {
		color: #4ade80;
		border-color: #1f6b3d;
		background: #0f2a1a;
	}
	.st-verified {
		color: #4ade80;
		border-color: #4ade80;
		background: #0f2a1a;
	}
	@keyframes pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.45;
		}
	}
</style>
