<script lang="ts">
	import type { TrackCloudView } from './track-cloud-state';

	let { view }: { view: TrackCloudView } = $props();

	// Tidal's mark is black and white, so its glyph takes a theme-aware color
	// from the `.tidal` rule below instead of the dim column text.
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
				<!-- Hand-drawn Spotify mark: green disk with three dark sound-wave
				     arcs, widest on top, so it reads as the provider and not as a
				     status dot. -->
				<circle cx="8" cy="8" r="7" fill={providerColor} />
				<path class="spotify-arc" d="M3.9 6.2Q8 4.6 12.1 6.6" fill="none" stroke="#191414" stroke-width="1.4" stroke-linecap="round" />
				<path class="spotify-arc" d="M4.5 8.5Q8 7.3 11.3 8.9" fill="none" stroke="#191414" stroke-width="1.15" stroke-linecap="round" />
				<path class="spotify-arc" d="M5.1 10.7Q8 9.8 10.5 11" fill="none" stroke="#191414" stroke-width="0.95" stroke-linecap="round" />
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
				<path class="tidal" d="M4 4l4 4-4 4M8 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.4" />
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
	.tidal {
		color: #fff;
	}
	:global(html[data-theme='light']) .tidal {
		color: #000;
	}
</style>
