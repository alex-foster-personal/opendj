<script lang="ts">
	/**
	 * WAVEQS-8348c: the ONE place a settings row's live preview is chosen. The
	 * Settings overlay and the waveform quick-settings popover both render a
	 * catalog row's preview through this component, so the same row shows the
	 * identical preview in both, painted by the same painter (CHROME-16/18).
	 * A preview kind with no branch throws rather than rendering nothing.
	 */
	import type { SettingPreview } from '$lib/settings/catalog-types';
	import WaveformDesignPreview from './WaveformDesignPreview.svelte';

	/** `label` names the preview for screen readers: two previewed rows must not share one name. */
	let { kind, label }: { kind: SettingPreview; label: string } = $props();

	$effect(() => {
		if (kind !== 'waveform') throw new Error(`settings preview: no renderer for ${String(kind)}`);
	});
</script>

{#if kind === 'waveform'}
	<WaveformDesignPreview {label} />
{/if}
