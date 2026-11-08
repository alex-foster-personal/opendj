<script lang="ts">
	// Far-left browser icon rail (SCREENSHOT-SPEC 5a). W17 (UX-RAIL-01):
	// only entries that do something, or a real planned feature, stay. The
	// top-left entry returns the active pane to the whole collection; the
	// rekordbox decorations with no feature behind them (bookmarks, explorer,
	// iTunes, file browser, Beatport, video, cloud lock and the Collection /
	// iTunes / Devices text tabs) are gone.
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import LibrarySourceMenu from './LibrarySourceMenu.svelte';

	/** The bottom bar's export/eject wording (BrowserPanel.svelte), so both entry points say the same thing. */
	const USB_EXPORT_TIP = 'Export / eject - open the USB screen to write an export to a stick or eject it';

	interface RailIcon {
		tip?: string;
		plannedId?: string;
		color: string;
		d: string;
		mode: 'fill' | 'stroke';
		source?: 'spotify';
		action?: 'record' | 'collection' | 'usb';
	}

	let {
		source,
		onspotify,
		oncollection,
		recording = false,
		recordingBusy = false,
		recordingWaiting = false,
		recordingTip = null,
		onrecord = () => {},
		onusb = () => {},
		recordable = true,
		recordingAvailable = true
	}: {
		source: 'collection' | 'spotify';
		onspotify: () => void;
		/** Top-left home entry: back to the whole collection (All Tracks). */
		oncollection: (event: MouseEvent) => void;
		recording?: boolean;
		recordingBusy?: boolean;
		/** REC pressed, but audio is not being written yet (mic prompt up). */
		recordingWaiting?: boolean;
		/** Overrides the REC tooltip, e.g. while waiting for permission. */
		recordingTip?: string | null;
		onrecord?: () => void;
		/** USBEXP-9746: opens the USB export screen (the bottom bar's export/eject path). */
		onusb?: () => void;
		/** False hides REC whatever the recording flag says (Library mode). */
		recordable?: boolean;
		/** Whether the build allows recording (the sets.recording flag). The parent reads the flag, so this component never touches the backend while it renders. */
		recordingAvailable?: boolean;
	} = $props();

	const ICONS: RailIcon[] = [
		{
			// House glyph, top-left: the way back to every track (UX-RAIL-01).
			tip: 'Collection - show every track in the library (All Tracks)',
			color: 'currentColor',
			d: 'M2.5 8L8 3l5.5 5M4 7v6.5h3V10h2v3.5h3V7',
			mode: 'stroke',
			action: 'collection'
		},
		{
			tip: 'Spotify playlists and acquisition queue',
			color: 'var(--rb-rail-spotify)',
			d: 'M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2z',
			mode: 'fill',
			source: 'spotify'
		},
		{
			// save/SD card: notched-corner card with contact pins (SCREENSHOT-SPEC 5a).
			// USBEXP-9746: opens the built USB export screen, like the bottom bar's export/eject.
			tip: USB_EXPORT_TIP,
			color: 'currentColor',
			d: 'M5 2h6.5L13 3.5V14H5zM6.2 3.2v2.6M8 3.2v2.6M9.8 3.2v2.6M11.6 3.7v2.1',
			mode: 'stroke',
			action: 'usb'
		},
		{
			// timer/stopwatch: crown button + dial hand (SCREENSHOT-SPEC 5a).
			// Planned entry (PARITY-TODO, id play-history): set sessions exist in
			// apps/sets; the in-browser list is not built, so this glyph stays inert.
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

	const SPOTIFY_ARCS_D = 'M4.6 6.6C6.8 5.9 9.4 6.1 11.4 7.2M5 8.6C6.8 8.1 8.9 8.3 10.5 9.2M5.4 10.5C6.8 10.1 8.4 10.3 9.6 11';
</script>

<nav class="rail" aria-label="browser sources" data-menu-target="library-source">
	<LibrarySourceMenu>
	{#each ICONS.filter((entry) => entry.action !== 'record' || (recordable && recordingAvailable)) as icon, i (i)}
		{@const isRecord = icon.action === 'record'}
		{@const isHome = icon.action === 'collection'}
		{@const isUsb = icon.action === 'usb'}
		{@const isInert = icon.source === undefined && icon.action === undefined}
		{@const tip = isRecord
			? (recordingTip ??
				(recording ? 'Stop set recording' : (icon.tip ?? 'Start set recording')))
			: isInert && icon.plannedId !== undefined
				? plannedTitle(icon.plannedId)
				: (icon.tip ?? '')}
		<button
			class="rail-btn"
			data-testid={isHome ? 'rail-collection-home' : isUsb ? 'rail-usb-export' : undefined}
			class:rb-inert={isInert}
			class:recording={isRecord && recording}
			class:waiting={isRecord && recordingWaiting}
			class:active={icon.source === 'spotify' && source === 'spotify'}
			disabled={isInert || (isRecord && recordingBusy)}
			title={tip}
			aria-label={tip}
			aria-pressed={icon.source === 'spotify' ? source === 'spotify' : isRecord ? recording : undefined}
			onclick={isHome ? oncollection : icon.source === 'spotify' ? onspotify : isRecord ? onrecord : isUsb ? onusb : undefined}
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
	</LibrarySourceMenu>
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
</style>
