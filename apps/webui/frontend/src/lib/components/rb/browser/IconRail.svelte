<script lang="ts">
	// Far-left browser icon rail (SCREENSHOT-SPEC 5a). Spotify is the only
	// active source at this stage; all other source glyphs remain inert.
	interface RailIcon {
		tip: string;
		color: string;
		d: string;
		mode: 'fill' | 'stroke';
		source?: 'spotify';
	}

	let { source, onspotify }: { source: 'collection' | 'spotify'; onspotify: () => void } = $props();

	const TIP = 'not implemented - see PARITY-TODO';

	const ICONS: RailIcon[] = [
		{ tip: TIP, color: 'currentColor', d: 'M4 2h8v12l-4-3.2L4 14z', mode: 'fill' },
		{ tip: TIP, color: 'currentColor', d: 'M3 3h4v4H3zM9 3h4v4H9zM3 9h4v4H3zM9 9h4v4H9z', mode: 'fill' },
		{ tip: TIP, color: 'currentColor', d: 'M2 3h12v2H2zM4 7h10v2H4zM6 11h8v2H6z', mode: 'fill' },
		{
			tip: 'Spotify playlists and acquisition queue',
			color: '#35c04f',
			d: 'M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2z',
			mode: 'fill',
			source: 'spotify'
		},
		{ tip: TIP, color: '#3d7dd9', d: 'M4 2h6l3 3v9H4z', mode: 'fill' },
		{ tip: TIP, color: '#8e5bd6', d: 'M8 2l6 6-6 6-6-6z', mode: 'fill' },
		{ tip: TIP, color: 'currentColor', d: 'M2 3h12v8H2zM6 12h4v2H6z', mode: 'fill' },
		{
			// save/SD card: notched-corner card with contact pins (SCREENSHOT-SPEC 5a)
			tip: TIP,
			color: 'currentColor',
			d: 'M5 2h6.5L13 3.5V14H5zM6.2 3.2v2.6M8 3.2v2.6M9.8 3.2v2.6M11.6 3.7v2.1',
			mode: 'stroke'
		},
		{
			tip: TIP,
			color: 'currentColor',
			d: 'M5.5 7V5.5a2.5 2.5 0 0 1 5 0V7M4 7h8v6H4z',
			mode: 'stroke'
		},
		{
			// timer/stopwatch: crown button + dial hand (SCREENSHOT-SPEC 5a)
			tip: TIP,
			color: 'currentColor',
			d: 'M6.5 1.5h3M8 1.5v2M8 3.5a5 5 0 1 1 0 10a5 5 0 1 1 0-10M8 8.5V5.5M8 8.5l2 1.5',
			mode: 'stroke'
		},
		{
			tip: `${TIP} (recordings - future apps/sets)`,
			color: '#d0342c',
			d: 'M8 3a5 5 0 1 1 0 10A5 5 0 1 1 8 3M8 7a1 1 0 1 1 0 2a1 1 0 1 1 0-2',
			mode: 'stroke'
		}
	];

	const SECTIONS: string[] = ['Collection', 'iTunes', 'Devices'];
</script>

<nav class="rail" aria-label="browser sources">
	{#each ICONS as icon, i (i)}
		<button
			class="rail-btn"
			class:rb-inert={icon.source === undefined}
			class:active={icon.source === 'spotify' && source === 'spotify'}
			disabled={icon.source === undefined}
			title={icon.tip}
			aria-label={icon.tip}
			aria-pressed={icon.source === 'spotify' ? source === 'spotify' : undefined}
			onclick={icon.source === 'spotify' ? onspotify : undefined}
		>
			<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
				{#if icon.mode === 'stroke'}
					<path d={icon.d} fill="none" stroke={icon.color} stroke-width="1.4" />
				{:else}
					<path d={icon.d} fill={icon.color} />
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
	.rail-btn.active {
		background: var(--rb-panel-raised);
		box-shadow: inset 2px 0 var(--rb-accent);
	}
	.rail-btn.rb-inert {
		cursor: default;
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
