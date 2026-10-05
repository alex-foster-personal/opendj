<script lang="ts">
	// Shared modal shell for the edit-suite batch (find-replace / bulk-edit /
	// mytag-editor). Net-new file - no hotspot ownership risk.
	import type { Snippet } from 'svelte';

	let {
		title,
		onclose,
		children
	}: {
		title: string;
		onclose: () => void;
		children: Snippet;
	} = $props();

	function onOverlayKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape') onclose();
	}
</script>

<svelte:window onkeydown={onOverlayKeydown} />

<div class="es-overlay" role="presentation" onclick={onclose}>
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<div
		class="es-panel"
		role="dialog"
		aria-modal="true"
		aria-label={title}
		tabindex="-1"
		onclick={(e) => e.stopPropagation()}
	>
		<div class="es-header">
			<h2>{title}</h2>
			<button class="es-close" onclick={onclose} aria-label="close">x</button>
		</div>
		<div class="es-body">
			{@render children()}
		</div>
	</div>
</div>

<style>
	.es-overlay {
		position: fixed;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.55);
		z-index: 200;
	}
	.es-panel {
		width: min(560px, 92vw);
		max-height: 82vh;
		display: flex;
		flex-direction: column;
		background: var(--rb-panel-raised, #1c1f24);
		border: 1px solid var(--rb-border, #333);
		border-radius: 4px;
		color: var(--rb-text, #ddd);
		font-family: var(--rb-font, inherit);
	}
	.es-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 14px;
		border-bottom: 1px solid var(--rb-border, #333);
	}
	.es-header h2 {
		font-family: var(--rb-font-brand);
		margin: 0;
		font-size: 13px;
		font-weight: 600;
	}
	.es-close {
		background: transparent;
		border: none;
		color: var(--rb-text-dim, #999);
		font-size: 14px;
		cursor: pointer;
		padding: 2px 6px;
	}
	.es-close:hover {
		color: var(--rb-text, #ddd);
	}
	.es-body {
		padding: 12px 14px;
		overflow: auto;
	}
</style>
