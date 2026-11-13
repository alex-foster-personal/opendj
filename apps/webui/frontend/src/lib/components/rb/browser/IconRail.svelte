<script lang="ts">
	// Far-left browser icon rail (SCREENSHOT-SPEC 5a). W17 (UX-RAIL-01):
	// only entries that do something, or a real planned feature, stay. The
	// top-left entry returns the active pane to the whole collection; the
	// rekordbox decorations with no feature behind them (bookmarks, explorer,
	// iTunes, file browser, Beatport, video, cloud lock and the Collection /
	// iTunes / Devices text tabs) are gone.
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import LibrarySourceMenu from './LibrarySourceMenu.svelte';
	import { tagListBadge, tagListRailTip } from '$lib/tag-list/tag-list-pane';
	import { EXPLORER_TIP } from '$lib/explorer/explorer-pane';

	/** The bottom bar's export/eject wording (BrowserPanel.svelte), so both entry points say the same thing. */
	const USB_EXPORT_TIP = 'Export / eject - open the USB screen to write an export to a stick or eject it';

	interface RailIcon {
		tip?: string;
		plannedId?: string;
		color: string;
		d: string;
		mode: 'fill' | 'stroke';
		source?: 'spotify';
		action?: 'record' | 'collection' | 'usb' | 'history' | 'tag_list' | 'explorer';
	}

	/** PLAYS-9745: the stopwatch entry's tooltip (rekordbox History, mimicked). */
	const HISTORY_TIP = 'History - every night you played, newest first; open one to see its tracks in play order and save it as a playlist';

	let {
		source,
		onspotify,
		oncollection,
		recording = false,
		recordingBusy = false,
		recordingWaiting = false,
		recordingTip = null,
		recordingStoppable = true,
		onrecord = () => {},
		onusb = () => {},
		historyOpen = false,
		onhistory = () => {},
		tagListCount = null,
		tagListOpen = false,
		ontaglist = () => {},
		explorerOpen = false,
		onexplorer = () => {},
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
		/** False when a lit REC must not offer stop (foreign-owned recording). */
		recordingStoppable?: boolean;
		onrecord?: () => void;
		/** USBEXP-9746: opens the USB export screen (the bottom bar's export/eject path). */
		onusb?: () => void;
		/** PLAYS-9745: the History list holds the tree slot (the stopwatch reads lit). */
		historyOpen?: boolean;
		/** PLAYS-9745: opens the play History list in the tree slot. */
		onhistory?: () => void;
		/** Issue #10028: tracks on the Tag List (the badge); null while unread. */
		tagListCount?: number | null;
		/** Issue #10028: the active pane shows the Tag List (the tag reads lit). */
		tagListOpen?: boolean;
		/** Issue #10028: opens the Tag List in the active pane. */
		ontaglist?: () => void;
		/** Issue #9747: the Explorer folder tree holds the tree slot (the folder reads lit). */
		explorerOpen?: boolean;
		/** Issue #9747: opens the Explorer folder tree in the tree slot. */
		onexplorer?: () => void;
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
			tip: 'Spotify playlists -- import, buy list, and side player',
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
			// PLAYS-9745: opens play History (one list per night) in the tree slot.
			tip: HISTORY_TIP,
			color: 'currentColor',
			d: 'M6.5 1.5h3M8 1.5v2M8 3.5a5 5 0 1 1 0 10a5 5 0 1 1 0-10M8 8.5V5.5M8 8.5l2 1.5',
			mode: 'stroke',
			action: 'history'
		},
		{
			// Issue #10028: price tag with a hole, rekordbox's Tag List entry (mimicked).
			color: 'currentColor',
			d: 'M2.5 2.5h5l6 6l-5 5l-6-6zM5.5 4.6a0.9 0.9 0 1 1 0 1.8a0.9 0.9 0 1 1 0-1.8',
			mode: 'stroke',
			action: 'tag_list'
		},
		{
			// Issue #9747: an open folder, rekordbox's Explorer entry (mimicked).
			tip: EXPLORER_TIP,
			color: 'currentColor',
			d: 'M1.5 4h4.5l1.5 1.5h7v7.5h-13zM1.5 7h13',
			mode: 'stroke',
			action: 'explorer'
		},
		{
			tip: 'Set recording - configure sources, then press Start recording',
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
		{@const isHistory = icon.action === 'history'}
		{@const isTagList = icon.action === 'tag_list'}
		{@const isExplorer = icon.action === 'explorer'}
		{@const isInert = icon.source === undefined && icon.action === undefined}
		{@const tip = isRecord
			? (recordingTip ??
				(recording
					? recordingStoppable
						? 'Stop set recording'
						: 'Recording owned elsewhere'
					: (icon.tip ?? 'Start set recording')))
			: isInert && icon.plannedId !== undefined
				? plannedTitle(icon.plannedId)
				: isTagList
					? tagListRailTip(tagListCount)
					: (icon.tip ?? '')}
		<button
			class="rail-btn"
			data-testid={isHome ? 'rail-collection-home' : isUsb ? 'rail-usb-export' : isHistory ? 'rail-history' : isTagList ? 'rail-tag-list' : isExplorer ? 'rail-explorer' : undefined}
			class:rb-inert={isInert}
			class:recording={isRecord && recording}
			class:waiting={isRecord && recordingWaiting}
			class:active={(icon.source === 'spotify' && source === 'spotify' && !historyOpen) || (isHistory && historyOpen) || (isTagList && tagListOpen) || (isExplorer && explorerOpen)}
			disabled={isInert || (isRecord && (recordingBusy || (recording && !recordingStoppable)))}
			title={tip}
			aria-label={tip}
			aria-pressed={icon.source === 'spotify' ? source === 'spotify' && !historyOpen : isRecord ? recording : isHistory ? historyOpen : isTagList ? tagListOpen : isExplorer ? explorerOpen : undefined}
			onclick={isHome ? oncollection : icon.source === 'spotify' ? onspotify : isHistory ? onhistory : isRecord ? onrecord : isUsb ? onusb : isTagList ? ontaglist : isExplorer ? onexplorer : undefined}
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
			{#if isTagList && tagListBadge(tagListCount) !== ''}
				<span class="rail-badge" data-testid="rail-tag-list-count">{tagListBadge(tagListCount)}</span>
			{/if}
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
	/* Issue #10028: the Tag List count, bottom-right over the tag glyph. */
	.rail-btn:has(.rail-badge) {
		position: relative;
	}
	.rail-badge {
		position: absolute;
		right: 0;
		bottom: 0;
		min-width: 9px;
		padding: 0 1px;
		border-radius: 3px;
		background: var(--rb-accent);
		color: var(--rb-on-accent, #fff);
		font-size: 7px;
		line-height: 9px;
		font-variant-numeric: tabular-nums;
		pointer-events: none;
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
	/* Blinking edge bar as a static box-shadow on ::after whose OPACITY
	 * animates (a box-shadow keyframe repainted the button every frame). */
	.rail-btn.waiting {
		position: relative;
		box-shadow: none;
	}
	.rail-btn.waiting::after {
		content: '';
		position: absolute;
		inset: 0;
		pointer-events: none;
		box-shadow: inset 2px 0 #d09a2c;
		animation: rec-waiting 1.2s ease-in-out infinite;
	}
	@keyframes rec-waiting {
		50% {
			opacity: 0;
		}
	}

	/* A11Y-11811: reduced motion (OS or Appearance); mirrors app.css. */
	@media (prefers-reduced-motion: reduce) {
		:where(*) {
			animation-duration: 0.01ms;
			animation-iteration-count: 1;
			transition-duration: 0.01ms;
		}
	}
	:global(html[data-reduce-motion]) :where(*) {
		animation-duration: 0.01ms;
		animation-iteration-count: 1;
		transition-duration: 0.01ms;
	}

</style>
