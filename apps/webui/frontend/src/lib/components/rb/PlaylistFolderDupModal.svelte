<script lang="ts">
	import type { UploadFileResult } from '$lib/rb/api-ingest';
	import type { PossibleDupDecision } from '$lib/rb/playlist-folder-drop';

	let {
		rows,
		busy = false,
		ondone,
		oncancel
	}: {
		rows: UploadFileResult[];
		busy?: boolean;
		ondone: (decisions: Map<string, PossibleDupDecision>) => void;
		oncancel: () => void;
	} = $props();

	let decisions = $state<Map<string, PossibleDupDecision>>(new Map());

	function setDecision(row: UploadFileResult, action: PossibleDupDecision): void {
		decisions = new Map(decisions).set(row.filename, action);
	}

	function finish(): void {
		const out = new Map<string, PossibleDupDecision>();
		for (const row of rows) {
			out.set(row.filename, decisions.get(row.filename) ?? 'reject');
		}
		ondone(out);
	}

	function onKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape') oncancel();
	}
</script>

<svelte:window onkeydown={onKeydown} />

<div class="scrim" role="presentation" onclick={oncancel} onkeydown={onKeydown}>
	<div
		class="modal"
		role="dialog"
		aria-label="Resolve possible duplicates"
		data-testid="playlist-folder-dup-modal"
		tabindex="-1"
		onclick={(e) => e.stopPropagation()}
		onkeydown={(e) => e.stopPropagation()}
	>
		<div class="m-title">Possible duplicates in folder drop</div>
		<ul class="m-files">
			{#each rows as r (r.filename)}
				<li>
					{r.filename}
					{#if r.duplicate_of !== null}
						- possible dup: {r.duplicate_of.title ?? r.duplicate_of.stable_id.slice(0, 8)}
						({r.duplicate_of.method}{r.duplicate_of.score !== null ? ` ${r.duplicate_of.score}` : ''})
					{/if}
					<span class="m-dup-actions">
						<button
							type="button"
							class="m-btn small"
							data-testid="ingest-dup-accept"
							disabled={busy}
							onclick={() => setDecision(r, 'accept')}
						>
							Accept as new
						</button>
						<button
							type="button"
							class="m-btn small"
							data-testid="ingest-dup-reject"
							disabled={busy}
							onclick={() => setDecision(r, 'reject')}
						>
							Skip
						</button>
					</span>
				</li>
			{/each}
		</ul>
		<div class="m-actions">
			<button type="button" class="m-btn" disabled={busy} onclick={finish}>Continue</button>
			<button type="button" class="m-btn ghost" disabled={busy} onclick={oncancel}>Cancel</button>
		</div>
	</div>
</div>

<style>
	.scrim {
		position: fixed;
		inset: 0;
		z-index: 80;
		background: rgb(0 0 0 / 45%);
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.modal {
		background: var(--panel-bg, #1a1a1a);
		border: 1px solid rgb(255 255 255 / 12%);
		border-radius: 8px;
		padding: 16px;
		min-width: 320px;
		max-width: 520px;
		max-height: 70vh;
		overflow: auto;
		color: var(--text, #eee);
	}
	.m-title {
		font-weight: 600;
		margin-bottom: 12px;
	}
	.m-files {
		list-style: none;
		padding: 0;
		margin: 0 0 12px;
		font-size: 12px;
	}
	.m-files li {
		padding: 6px 0;
		border-bottom: 1px solid rgb(255 255 255 / 8%);
	}
	.m-dup-actions {
		display: block;
		margin-top: 4px;
	}
	.m-actions {
		display: flex;
		gap: 8px;
		justify-content: flex-end;
	}
	.m-btn {
		padding: 6px 10px;
		border-radius: 4px;
		border: 1px solid rgb(255 255 255 / 20%);
		background: rgb(255 255 255 / 10%);
		color: inherit;
		cursor: pointer;
	}
	.m-btn.small {
		font-size: 11px;
		padding: 3px 8px;
		margin-right: 4px;
	}
	.m-btn.ghost {
		background: transparent;
	}
	.m-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
