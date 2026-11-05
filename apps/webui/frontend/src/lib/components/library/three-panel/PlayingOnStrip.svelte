<script lang="ts">
	// SKINLAYOUT-8869c: the thin white progress line and the green
	// "Playing on <device>" strip under the three-panel player. Library
	// previews play through the browser's default sink, so the label is that
	// sink's name when the browser exposes it.
	import { onMount } from 'svelte';
	import { libraryPreview } from '$lib/library/library-preview.svelte';
	import { playingOnLabel, progressRatio } from '$lib/rb/skin-layout';

	let sinkLabel = $state<string | null>(null);
	onMount(() => {
		navigator.mediaDevices?.enumerateDevices().then(
			(devices) => {
				sinkLabel =
					devices.find((d) => d.kind === 'audiooutput' && d.deviceId === 'default')?.label ?? null;
			},
			(exc: unknown) => console.error('[playing-on] output list unavailable:', exc)
		);
	});
	const ratio = $derived(progressRatio(libraryPreview.position_ms, libraryPreview.duration_ms));
</script>

<div class="player-foot">
	<div class="progress" title="Preview position in the track" data-testid="player-progress">
		<div class="progress-fill" style:width={`${(ratio * 100).toFixed(2)}%`}></div>
	</div>
	<p class="playing-on" data-testid="playing-on">{playingOnLabel(sinkLabel)}</p>
</div>

<style>
	.player-foot {
		grid-area: playingon;
	}
	.progress {
		height: 4px;
		margin: 0 16px 6px;
		border-radius: 2px;
		background: #4d4d4d;
	}
	.progress-fill {
		height: 100%;
		border-radius: 2px;
		background: #ffffff;
	}
	.playing-on {
		margin: 0;
		padding: 4px 16px;
		background: #1ed760;
		color: #000000;
		font-size: 12px;
		font-weight: 700;
	}
</style>
