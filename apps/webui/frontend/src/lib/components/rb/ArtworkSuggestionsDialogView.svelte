<!--
	LIBM-189: review store covers (Deezer / iTunes) before any is saved.
	Props only (one story per state); EnrichCard fetches and wires the buttons.
	Agent parity, one endpoint per control:
	  list          GET  /api/v1/artwork/suggestions             (opendj artwork list)
	  Save selected POST /api/v1/artwork/suggestions/decide {accept}      (opendj artwork accept)
	  Save all      POST /api/v1/artwork/suggestions/decide {accept_all}  (opendj artwork accept --all)
	  Skip          POST /api/v1/artwork/suggestions/decide {skip}        (opendj artwork skip)
	  Not now       closes; every cover stays pending
	Analysis never waits on this dialog: it runs in the engine.
-->
<script lang="ts">
	import type { ArtworkSuggestion } from '$lib/enrich/artwork-suggestions';

	interface Props {
		suggestions: ArtworkSuggestion[];
		note: string;
		/** stable_ids ticked; every cover starts ticked. */
		selected: ReadonlySet<string>;
		busy?: boolean;
		error?: string | null;
		ontoggle: (stableId: string) => void;
		onsaveselected: () => void;
		onsaveall: () => void;
		onskip: () => void;
		onclose: () => void;
	}

	const {
		suggestions,
		note,
		selected,
		busy = false,
		error = null,
		ontoggle,
		onsaveselected,
		onsaveall,
		onskip,
		onclose
	}: Props = $props();

	function onkeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') onclose();
	}
</script>

<svelte:window {onkeydown} />

<div class="art-backdrop" role="presentation" onclick={onclose}></div>
<div class="art-dialog" role="dialog" aria-modal="true" aria-labelledby="art-dialog-title" data-testid="artwork-suggestions">
	<header>
		<h3 id="art-dialog-title">Save these covers?*</h3>
		<button type="button" class="art-close" onclick={onclose} title="Close; every cover stays pending">Not now</button>
	</header>
	<p class="art-lead">
		MusicBrainz had no cover for these tracks; Deezer or iTunes has one. Nothing is saved unless you say so.
	</p>
	<ul class="art-list">
		{#each suggestions as s (s.stable_id)}
			<li>
				<label>
					<input
						type="checkbox"
						checked={selected.has(s.stable_id)}
						onchange={() => ontoggle(s.stable_id)}
						data-testid="artwork-suggestion-check"
					/>
					<img src={s.image_url} alt={`${s.source_label} cover for ${s.track_title}`} width="56" height="56" loading="lazy" />
					<span class="art-names">
						<span class="art-track">{s.track_artist} - {s.track_title}</span>
						<span class="art-hit" title={s.cover_url}>{s.source_label}: {s.hit_artist} - {s.hit_title}</span>
					</span>
				</label>
			</li>
		{/each}
	</ul>
	<div class="art-actions">
		<button type="button" class="art-go" onclick={onsaveselected} disabled={busy || selected.size === 0}>Save selected</button>
		<button type="button" onclick={onsaveall} disabled={busy}>Save all</button>
		<button
			type="button"
			onclick={onskip}
			disabled={busy || selected.size === 0}
			title="Never suggest a store cover for the ticked tracks again">Skip</button
		>
	</div>
	{#if error}<p class="art-error">{error}</p>{/if}
	<p class="art-note" data-testid="artwork-suggestions-note">{note}</p>
</div>

<style>
	.art-backdrop {
		position: fixed;
		inset: 0;
		background: rgb(0 0 0 / 45%);
		z-index: 1000;
	}
	.art-dialog {
		position: fixed;
		top: 50%;
		left: 50%;
		transform: translate(-50%, -50%);
		width: min(560px, calc(100vw - 32px));
		max-height: calc(100vh - 64px);
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 12px 14px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		z-index: 1001;
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}
	h3 {
		margin: 0;
		font-size: 1em;
	}
	.art-lead,
	.art-note {
		margin: 0;
		color: var(--rb-text-dim);
	}
	.art-list {
		list-style: none;
		margin: 0;
		padding: 0;
		overflow-y: auto;
	}
	.art-list label {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px 0;
		border-bottom: 1px solid var(--rb-border);
		cursor: pointer;
	}
	.art-list img {
		object-fit: cover;
		flex: none;
	}
	.art-names {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.art-track,
	.art-hit {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.art-hit {
		color: var(--rb-text-dim);
	}
	.art-actions {
		display: flex;
		gap: 6px;
	}
	.art-go {
		border-color: var(--rb-accent);
	}
	.art-error {
		margin: 0;
		color: var(--rb-danger, #e5484d);
	}
</style>
