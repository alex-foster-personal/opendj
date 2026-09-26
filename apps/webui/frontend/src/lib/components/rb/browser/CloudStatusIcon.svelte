<script lang="ts">
	import type { TrackCloudView } from './track-cloud-state';

	let { view }: { view: TrackCloudView } = $props();

	// Tidal has no distinct brand color here; it shares the same
	// `currentColor` fallback every other/unrecognized provider takes.
	const providerColor = $derived.by(() => {
		if (view.provider === 'spotify') return '#35c04f';
		if (view.provider === 'soundcloud') return '#ff5500';
		return 'currentColor';
	});
</script>

<span
	class="cloud-status-icon"
	class:streaming={view.kind === 'streaming'}
	class:not-on-cloud={view.kind === 'not-on-cloud'}
	class:on-cloud-not-local={view.kind === 'on-cloud-not-local'}
	class:on-cloud-and-local={view.kind === 'on-cloud-and-local'}
	title={view.title}
	aria-label={view.title}
>
	{#if view.kind === 'streaming' && view.provider !== null && view.provider !== 'unknown'}
		<svg class="provider-icon" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
			{#if view.provider === 'spotify'}
				<circle cx="8" cy="8" r="6" fill={providerColor} />
			{:else if view.provider === 'soundcloud'}
				<path
					d="M2 10h2l1-4 1.5 6 1-3 1 3h6"
					fill="none"
					stroke={providerColor}
					stroke-width="1.2"
					stroke-linecap="round"
					stroke-linejoin="round"
				/>
			{:else if view.provider === 'tidal'}
				<path d="M4 4l4 4-4 4M8 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.4" />
			{/if}
		</svg>
	{:else}
		<svg class="cloud-icon" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
			<path
				d="M4.5 12a3 3 0 0 1-.4-5.97A4 4 0 0 1 12 6.5 2.75 2.75 0 0 1 11.5 12z"
				fill={view.kind === 'not-on-cloud' ? 'none' : 'currentColor'}
				stroke="currentColor"
				stroke-width="1.25"
			/>
			{#if view.kind === 'not-on-cloud'}
				<path
					d="M3 13 13 3"
					fill="none"
					stroke="currentColor"
					stroke-width="1.5"
					stroke-linecap="round"
				/>
			{/if}
		</svg>
	{/if}
	{#if view.overlay === 'green-tick'}
		<svg class="tick-overlay tick-green" viewBox="0 0 8 8" width="6" height="6" aria-hidden="true">
			<path d="M1.2 4.2 L3.2 6.2 L6.8 1.8" fill="none" stroke="#35c04f" stroke-width="1.4" stroke-linecap="round" />
		</svg>
	{:else if view.overlay === 'blue-tick'}
		<svg class="tick-overlay tick-blue" viewBox="0 0 8 8" width="6" height="6" aria-hidden="true">
			<path d="M1.2 4.2 L3.2 6.2 L6.8 1.8" fill="none" stroke="#4cc9f0" stroke-width="1.4" stroke-linecap="round" />
		</svg>
	{/if}
</span>

<style>
	.cloud-status-icon {
		position: relative;
		display: inline-flex;
		align-items: center;
		justify-content: center;
	}
	.tick-overlay {
		position: absolute;
		right: -2px;
		bottom: -1px;
	}
	.on-cloud-not-local {
		color: #e5484d;
	}
</style>
