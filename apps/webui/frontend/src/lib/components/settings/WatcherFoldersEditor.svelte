<script lang="ts">
	// LIBM-129: the watcher-folders path_lines row of the settings overlay. Its
	// own component so the overlay loads it on demand (dynamic import): the
	// overlay itself rides the library page's first paint, and this row is only
	// ever drawn while settings are open (bundle budget, PR #4014). Mounted
	// fresh on every open, so its draft re-seeds from the saved prefs each time.
	import type { SettingDef } from '$lib/settings/catalog-types';
	import {
		formatWatcherFolderLines,
		parseWatcherFolderLines,
		validateWatcherFoldersExist
	} from '$lib/rb/library-watcher-folders';
	import { setLibraryWatcherFolders, uiPrefs } from '$lib/rb/prefs.svelte';

	let { def }: { def: SettingDef } = $props();

	let draft = $state(formatWatcherFolderLines(uiPrefs.library_watcher_folders));
	let busy = $state(false);
	let msg = $state<string | null>(null);
	const v2Notice = $derived(def.control.kind === 'path_lines' ? def.control.v2Notice : '');

	async function apply(): Promise<void> {
		busy = true;
		msg = null;
		try {
			const paths = parseWatcherFolderLines(draft);
			await validateWatcherFoldersExist(paths);
			setLibraryWatcherFolders(paths);
			draft = formatWatcherFolderLines(paths);
			msg = paths.length === 0 ? 'Cleared watcher folders.' : `Saved ${paths.length} folder(s).`;
		} catch (err) {
			msg = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

<div class="so-path-lines" title={def.title}>
	<p class="so-v2-notice" title={v2Notice}>
		{v2Notice}
	</p>
	<textarea aria-label={def.label} rows={3} bind:value={draft}></textarea>
	<button
		type="button"
		disabled={busy}
		title="Validate paths exist on disk, then save"
		onclick={() => void apply()}
	>
		{busy ? 'Saving…' : 'Apply'}
	</button>
	{#if msg}
		<span class="so-path-lines-msg" title={msg}>{msg}</span>
	{/if}
</div>
