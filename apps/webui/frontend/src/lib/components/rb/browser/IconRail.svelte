<script lang="ts">
	// Far-left browser icon rail (SCREENSHOT-SPEC 5a). Spotify is the only
	// active source at this stage; all other source glyphs remain inert.
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import { setRecordingEnabled } from '$lib/sets/recording-flag';

	interface RailIcon {
		tip?: string;
		plannedId?: string;
		color: string;
		d: string;
		mode: 'fill' | 'stroke';
		source?: 'spotify';
		action?: 'record';
	}

	let {
		source,
		onspotify,
		recording = false,
		recordingBusy = false,
		recordingWaiting = false,
		recordingTip = null,
		onrecord = () => {}
	}: {
		source: 'collection' | 'spotify';
		onspotify: () => void;
		recording?: boolean;
		recordingBusy?: boolean;
		/** REC pressed, but audio is not being written yet (mic prompt up). */
		recordingWaiting?: boolean;
		/** Overrides the REC tooltip, e.g. while waiting for permission. */
		recordingTip?: string | null;
		onrecord?: () => void;
	} = $props();

	const ICONS: RailIcon[] = [
		{ plannedId: 'collection-bookmarks', color: 'currentColor', d: 'M4 2h8v12l-4-3.2L4 14z', mode: 'fill' },
		{ plannedId: 'explorer-grid', color: 'currentColor', d: 'M3 3h4v4H3zM9 3h4v4H9zM3 9h4v4H3zM9 9h4v4H9z', mode: 'fill' },
		{ plannedId: 'itunes-library', color: 'currentColor', d: 'M2 3h12v2H2zM4 7h10v2H4zM6 11h8v2H6z', mode: 'fill' },
		{
			tip: 'Spotify playlists and acquisition queue',
			color: 'var(--rb-rail-spotify)',
			d: 'M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2z',
			mode: 'fill',
			source: 'spotify'
		},
		{ plannedId: 'file-browser', color: 'var(--rb-rail-files)', d: 'M4 2h6l3 3v9H4z', mode: 'fill' },
		{ plannedId: 'beatport', color: 'var(--rb-rail-beatport)', d: 'M8 2l6 6-6 6-6-6z', mode: 'fill' },
		{ plannedId: 'video-output', color: 'currentColor', d: 'M2 3h12v8H2zM6 12h4v2H6z', mode: 'fill' },
		{
			// save/SD card: notched-corner card with contact pins (SCREENSHOT-SPEC 5a)
			plannedId: 'usb-export',
			color: 'currentColor',
			d: 'M5 2h6.5L13 3.5V14H5zM6.2 3.2v2.6M8 3.2v2.6M9.8 3.2v2.6M11.6 3.7v2.1',
			mode: 'stroke'
		},
		{
			plannedId: 'cloud-lock',
			color: 'currentColor',
			d: 'M5.5 7V5.5a2.5 2.5 0 0 1 5 0V7M4 7h8v6H4z',
			mode: 'stroke'
		},
		{
			// timer/stopwatch: crown button + dial hand (SCREENSHOT-SPEC 5a)
			plannedId: 'play-history',
			color: 'currentColor',
			d: 'M6.5 1.5h3M8 1.5v2M8 3.5a5 5 0 1 1 0 10a5 5 0 1 1 0-10M8 8.5V5.5M8 8.5l2 1.5',
			mode: 'stroke'
		},
		{
			tip: 'Start set recording',
			color: 'var(--rb-rail-record)',
			d: 'M8 3a5 5 0 1 1 0 10A5 5 0 1 1 8 3M8 7a1 1 0 1 1 0 2a1 1 0 1 1 0-2',
			mode: 'stroke',
			action: 'record'
		}
	];

	const SECTIONS: string[] = ['Collection', 'iTunes', 'Devices'];
	const SPOTIFY_ARCS_D = 'M4.6 6.6C6.8 5.9 9.4 6.1 11.4 7.2M5 8.6C6.8 8.1 8.9 8.3 10.5 9.2M5.4 10.5C6.8 10.1 8.4 10.3 9.6 11';
</script>

<nav class="rail" aria-label="browser sources">
	{#each ICONS.filter((entry) => entry.action !== 'record' || setRecordingEnabled()) as icon, i (i)}
		{@const isRecord = icon.action === 'record'}
		{@const isInert = icon.source === undefined && icon.action === undefined}
		{@const tip = isRecord
			? (recordingTip ??
				(recording ? 'Stop set recording' : (icon.tip ?? 'Start set recording')))
			: isInert && icon.plannedId !== undefined
				? plannedTitle(icon.plannedId)
				: (icon.tip ?? '')}
		<button
			class="rail-btn"
			class:rb-inert={isInert}
			class:recording={isRecord && recording}
			class:waiting={isRecord && recordingWaiting}
			class:active={icon.source === 'spotify' && source === 'spotify'}
			disabled={isInert || (isRecord && recordingBusy)}
			title={tip}
			aria-label={tip}
			aria-pressed={icon.source === 'spotify' ? source === 'spotify' : isRecord ? recording : undefined}
			onclick={icon.source === 'spotify' ? onspotify : isRecord ? onrecord : undefined}
		>
			<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
				{#if icon.mode === 'stroke'}
					<path d={icon.d} fill="none" style:stroke={icon.color} stroke-width="1.4" />
				{:else}
					<path d={icon.d} style:fill={icon.color} />
				{/if}
				{#if icon.source === 'spotify'}
					<!-- Glyph arcs: transparent by default, the panel color under
					     mono-dev so the disc reads as the Spotify glyph in rail gray. -->
					<path class="spotify-arcs" d={SPOTIFY_ARCS_D} fill="none" stroke-width="1.1" stroke-linecap="round" />
				{/if}
			</svg>
		</button>
	{/each}
	{#each SECTIONS as label (label)}
		<span class="rail-label">{label}</span>
	{/each}
</nav>

<style>
	.rail {
		grid-area: rail;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 2px;
		padding: 4px 0;
		background: var(--rb-panel);
		border-right: 1px solid var(--rb-border);
		overflow: hidden;
	}
	.rail-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 24px;
		height: 22px;
		padding: 0;
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.spotify-arcs {
		stroke: var(--rb-rail-spotify-arc);
	}
	.rail-btn.active {
		background: var(--rb-panel-raised);
		box-shadow: inset 2px 0 var(--rb-accent);
	}
	.rail-btn.rb-inert {
		cursor: default;
	}
	.rail-btn.recording {
		background: #4a1717;
		box-shadow: inset 2px 0 #d0342c;
	}
	.rail-btn.waiting {
		box-shadow: inset 2px 0 #d09a2c;
		animation: rec-waiting 1.2s ease-in-out infinite;
	}
	@keyframes rec-waiting {
		50% {
			box-shadow: inset 2px 0 transparent;
		}
	}
	/* Horizontal section labels below the rail groups (SCREENSHOT-SPEC 5a) -
	 * NOT rotated; the rail is narrow so the type is tiny like rekordbox's. */
	.rail-label {
		width: 100%;
		font-size: 6px;
		line-height: 1.2;
		text-align: center;
		color: var(--rb-text-dim);
		margin-top: 10px;
		letter-spacing: -0.3px;
		white-space: nowrap;
		overflow: hidden;
	}
</style>
