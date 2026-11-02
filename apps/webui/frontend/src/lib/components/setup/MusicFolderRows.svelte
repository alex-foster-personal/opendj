<script lang="ts">
	import { folderVerdict } from '$lib/setup/setup-api';
	import { shortenPath } from '$lib/setup/detect-view';
	import type { FolderRow } from '$lib/setup/folder-rows';

	type FolderCandidate = { path: string; readable: boolean; detail: string };

	let {
		rows,
		busy = false,
		refusal = null,
		nativeFolderPicker = false,
		candidates = [],
		onPick,
		onCheck,
		onRemove,
		onAdd,
		onSuggestion
	}: {
		rows: FolderRow[];
		busy?: boolean;
		refusal?: string | null;
		nativeFolderPicker?: boolean;
		candidates?: FolderCandidate[];
		onPick: (id: string) => void;
		onCheck: (id: string) => void;
		onRemove: (id: string) => void;
		onAdd: () => void;
		onSuggestion: (path: string) => void;
	} = $props();
</script>

{#if candidates.length > 0}
	<div class="folder-suggestions">
		{#each candidates as candidate (candidate.path)}
			{#if candidate.readable}
				<button
					type="button"
					class="folder-chip"
					onclick={() => onSuggestion(candidate.path)}
					disabled={busy || refusal !== null}
					title="Use {shortenPath(candidate.path)}"
				>
					{shortenPath(candidate.path)}
				</button>
			{:else}
				<span class="folder-chip refused" title="This folder cannot be read yet" data-agent-detail={candidate.detail}>
					{shortenPath(candidate.path)}
				</span>
			{/if}
		{/each}
	</div>
{/if}

<div class="folder-rows">
	{#each rows as row (row.id)}
		<div class="folder-row">
			<div class="folder-path-row">
				<input type="text" placeholder="/Users/you/Music" bind:value={row.path} aria-label="Folder to import" />
				<button
					type="button"
					class="folder-pick"
					onclick={() => onPick(row.id)}
					disabled={busy || refusal !== null || !nativeFolderPicker}
					title={nativeFolderPicker ? 'Choose a folder' : 'Choose a folder (available in the Open DJ desktop app)'}
					aria-label="Choose a folder"
				>
					Choose
				</button>
			</div>
			<div class="folder-row-actions">
				<button type="button" onclick={() => onCheck(row.id)} disabled={busy || refusal !== null} title={refusal ?? 'Look inside this folder without importing it'}>
					Check this folder
				</button>
				<button type="button" class="secondary folder-remove" onclick={() => onRemove(row.id)} disabled={busy || refusal !== null || rows.length <= 1} aria-label="Remove folder" title={rows.length <= 1 ? 'Keep at least one music folder' : 'Remove this folder row'}>
					Remove
				</button>
			</div>
			{#if row.scan !== null}
				{#if row.scan.denied}
					<p class="fatal" role="alert">{folderVerdict(row.scan)}</p>
				{:else}
					<p class="verdict" role="status">{folderVerdict(row.scan)}</p>
				{/if}
				{#if row.scan.readable}
					<p class="counts"><strong>{row.scan.audio_files} audio files</strong>{#if row.scan.icloud_placeholders > 0}, <strong>{row.scan.icloud_placeholders} iCloud placeholders skipped</strong>{/if}</p>
					<ul class="probes">{#each row.scan.sample as example (example)}<li><code>{shortenPath(example)}</code></li>{/each}</ul>
				{/if}
			{/if}
		</div>
	{/each}
</div>

{#if rows.some((row) => row.path.trim() !== '')}
	<button type="button" class="secondary folder-add" onclick={onAdd} disabled={busy || refusal !== null} aria-label="Add another folder" title="Add another music folder">+</button>
{/if}

<style>
	.folder-rows { display: grid; gap: 0.75rem; }
	.folder-path-row { display: flex; gap: 0; }
	.folder-path-row input {
		flex: 1;
		min-width: 0;
		border-top-right-radius: 0;
		border-bottom-right-radius: 0;
		border-right: none;
	}
	.folder-path-row input::placeholder {
		color: var(--muted);
		opacity: 0.65;
		font-style: italic;
	}
	.folder-pick {
		margin-left: -1px;
		border-top-left-radius: 0;
		border-bottom-left-radius: 0;
	}
	.folder-row-actions { display: flex; gap: 0.35rem; margin-top: 0.35rem; }
	.folder-suggestions { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.6rem; }
	.folder-chip { cursor: pointer; }
	.folder-chip.refused { cursor: not-allowed; opacity: 0.7; }
	.folder-add { margin-top: 0.5rem; }
	.counts, .probes { color: var(--muted); font-size: 0.85rem; }
	.probes { margin: 0.25rem 0 0; }
	.fatal { color: var(--danger); }
</style>
