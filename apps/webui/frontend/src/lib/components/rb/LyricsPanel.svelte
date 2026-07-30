<script lang="ts">
	// Unsynced lyrics panel (LRCLIB cache). Word-time sync is a later spike.
	import type { LyricsPayload } from '$lib/rb/api-rb';

	let {
		lyrics,
		onclose
	}: {
		lyrics: LyricsPayload | null;
		onclose: () => void;
	} = $props();
</script>

{#if lyrics !== null}
	<div class="lyrics-panel" role="dialog" aria-label="Lyrics">
		<header>
			<div class="meta">
				<strong>{lyrics.title}</strong>
				<span>{lyrics.artist}</span>
				<em>{lyrics.source} · unsynced</em>
			</div>
			<button type="button" class="close" onclick={onclose} aria-label="Close lyrics">×</button>
		</header>
		<pre class="body">{lyrics.plain}</pre>
	</div>
{/if}

<style>
	.lyrics-panel {
		position: fixed;
		top: 48px;
		right: 12px;
		z-index: 9000;
		width: min(360px, calc(100vw - 24px));
		max-height: min(70vh, 640px);
		display: flex;
		flex-direction: column;
		background: var(--rb-panel, #14171d);
		border: 1px solid var(--rb-border, #2a3038);
		border-radius: 4px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55);
		color: var(--rb-text, #c8cdd2);
		font-family: var(--rb-font, ui-sans-serif, system-ui, sans-serif);
	}
	header {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		padding: 10px 12px;
		border-bottom: 1px solid var(--rb-border, #2a3038);
	}
	.meta {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
		flex: 1;
	}
	.meta strong {
		font-size: 13px;
		color: var(--rb-text, #e8ecf0);
	}
	.meta span,
	.meta em {
		font-size: 11px;
		color: var(--rb-text-dim, #7a8088);
		font-style: normal;
	}
	.close {
		border: none;
		background: transparent;
		color: var(--rb-text-dim, #7a8088);
		font-size: 18px;
		line-height: 1;
		cursor: pointer;
		padding: 0 2px;
	}
	.close:hover {
		color: var(--rb-text, #e8ecf0);
	}
	.body {
		margin: 0;
		padding: 12px;
		overflow: auto;
		white-space: pre-wrap;
		font-size: 12px;
		line-height: 1.45;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
</style>
