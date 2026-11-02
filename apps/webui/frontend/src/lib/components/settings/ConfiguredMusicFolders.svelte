<script lang="ts">
	import { onMount } from 'svelte';
	import MusicFolderRows from '$lib/components/setup/MusicFolderRows.svelte';
	import { folderAdvanceRefusal, newFolderRow, type FolderRow } from '$lib/setup/folder-rows';
	import {
		finalSetupRefusal,
		getConfiguredFolders,
		scanFolder,
		setConfiguredFolders
	} from '$lib/setup/setup-api';
	import { nativeShellKind, pickFolder } from '$lib/shell/native-shell';

	let rows = $state<FolderRow[]>([newFolderRow()]);
	let busy = $state(false);
	let message = $state<string | null>(null);
	const refusal = $derived(finalSetupRefusal());

	async function rowsFromConfiguredPaths(folders: string[]): Promise<FolderRow[]> {
		const loaded: FolderRow[] = [];
		for (const path of folders) {
			const row: FolderRow = { ...newFolderRow(), path };
			try {
				row.scan = await scanFolder(path);
			} catch {
				// Leave scan null; the operator can re-check before saving edits.
			}
			loaded.push(row);
		}
		return loaded.length > 0 ? loaded : [newFolderRow()];
	}

	async function load(): Promise<void> {
		busy = true;
		try {
			rows = await rowsFromConfiguredPaths(await getConfiguredFolders());
			message = null;
		} catch (exc) {
			message = `Could not load configured music folders: ${String(exc)}`;
		} finally {
			busy = false;
		}
	}

	async function pick(id: string): Promise<void> {
		if (nativeShellKind() === null) return;
		const selected = await pickFolder('Choose a music folder');
		if (typeof selected !== 'string') return;
		rows = rows.map((row) => row.id === id ? { ...row, path: selected, scan: null } : row);
	}

	async function check(id: string): Promise<void> {
		const row = rows.find((entry) => entry.id === id);
		if (row === undefined || row.path.trim() === '') {
			message = 'Type a folder path first.';
			return;
		}
		busy = true;
		try {
			const scan = await scanFolder(row.path);
			rows = rows.map((entry) => entry.id === id ? { ...entry, path: scan.path, scan } : entry);
			message = null;
		} catch (exc) {
			message = `Could not check folder: ${String(exc)}`;
		} finally {
			busy = false;
		}
	}

	function remove(id: string): void {
		if (rows.length <= 1) {
			message = 'Keep at least one music folder configured.';
			return;
		}
		rows = rows.filter((row) => row.id !== id);
	}

	async function save(): Promise<void> {
		const refusalReason = folderAdvanceRefusal(rows);
		if (refusalReason !== null) {
			message = `Cannot save folders: ${refusalReason}.`;
			return;
		}
		busy = true;
		try {
			const folders = await setConfiguredFolders(rows.map((row) => row.path));
			rows = await rowsFromConfiguredPaths(folders);
			message = `Saved ${folders.length} music folder(s). Future rescans use this list.`;
		} catch (exc) {
			message = `Could not save music folders: ${String(exc)}`;
		} finally {
			busy = false;
		}
	}

	onMount(() => {
		void load();
	});
</script>

<p style="color: var(--muted);">These folders are used by the live library rescan. Add or remove folders here without repeating onboarding.</p>
<MusicFolderRows
	rows={rows}
	busy={busy}
	refusal={refusal}
	nativeFolderPicker={nativeShellKind() !== null}
	onPick={(id) => void pick(id)}
	onCheck={(id) => void check(id)}
	onRemove={remove}
	onAdd={() => rows = [...rows, newFolderRow()]}
	onSuggestion={(path) => rows = [...rows, { ...newFolderRow(), path }]}
/>
<button type="button" onclick={() => void save()} disabled={busy || refusal !== null} title="Save the checked folder list">Save music folders</button>
{#if message}<p role="status">{message}</p>{/if}
