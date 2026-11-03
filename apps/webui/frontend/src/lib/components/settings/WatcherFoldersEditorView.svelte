<script lang="ts">
	// Props-only half of WatcherFoldersEditor: the draft is bindable and Apply
	// is a callback, so the container owns the prefs read and the save.
	import type { SettingDef } from '$lib/settings/catalog-types';

	let {
		def,
		draft = $bindable(),
		busy,
		msg,
		onApply
	}: {
		def: SettingDef;
		draft: string;
		busy: boolean;
		msg: string | null;
		onApply: () => void;
	} = $props();

	const notice = $derived(def.control.kind === 'path_lines' ? def.control.notice : '');
</script>

<div class="so-path-lines" title={def.title}>
	<p class="so-v2-notice" title={notice}>
		{notice}
	</p>
	<textarea aria-label={def.label} rows={3} bind:value={draft}></textarea>
	<button
		type="button"
		disabled={busy}
		title="Validate paths exist on disk, then save"
		onclick={onApply}
	>
		{busy ? 'Saving…' : 'Apply'}
	</button>
	{#if msg}
		<span class="so-path-lines-msg" title={msg}>{msg}</span>
	{/if}
</div>
