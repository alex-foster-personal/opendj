<script lang="ts">
	/**
	 * LIBUX-06: a skill-tree-style radial explorer of the real library.
	 * Rings are a plain SVG sunburst (family, then genre, then one wedge per
	 * track) with mechanical proportions only -- no hand-tuned spacing or
	 * animation. All state routes through GET /api/v1/library/wheel for
	 * agent parity (same endpoint the CLI at `python -m apps.library_wheel`
	 * calls).
	 */
	import { onMount } from 'svelte';
	import { fetchLibraryWheel } from './wheel-api';
	import { buildWheelLayout, wedgePath, type WheelWedge } from './wheel-layout';
	import { type AxisKey, type LibraryWheelResponse } from './types';

	const VIEWBOX_SIZE = 420;
	const CENTER = VIEWBOX_SIZE / 2;
	const RADIUS_SCALE = CENTER - 20;

	let data = $state<LibraryWheelResponse | null>(null);
	let error = $state<string | null>(null);
	let loading = $state(true);
	let axis = $state<AxisKey>('play_count');
	let requestRevision = 0;

	async function load(): Promise<void> {
		const revision = ++requestRevision;
		loading = true;
		try {
			const response = await fetchLibraryWheel(axis);
			if (revision === requestRevision) {
				data = response;
				error = null;
			}
		} catch (caught) {
			if (revision === requestRevision) {
				error = caught instanceof Error ? caught.message : String(caught);
			}
		} finally {
			if (revision === requestRevision) loading = false;
		}
	}

	function applyAxis(event: Event): void {
		axis = (event.currentTarget as HTMLSelectElement).value as AxisKey;
		void load();
	}

	function wedgeTooltip(wedge: WheelWedge): string {
		if (!wedge.track) return wedge.title;
		const { title, artist, genre, axis_title: axisTitle } = wedge.track;
		const parts = [title ?? wedge.track.stable_id, artist ? `by ${artist}` : null, genre];
		const label = parts.filter((part): part is string => Boolean(part)).join(', ');
		return axisTitle ? `${label} (${axisTitle})` : label;
	}

	const layout = $derived(data ? buildWheelLayout(data) : null);

	onMount(() => void load());
</script>

<svelte:head>
	<title>Library wheel</title>
</svelte:head>

<section class="wheel-page">
	<header>
		<div>
			<p class="eyebrow">LIBUX-06</p>
			<h2>Library wheel</h2>
			<p class="subtitle">A radial explorer of the real library, grouped by genre family.</p>
		</div>
		<label>
			<span>Axis</span>
			<select value={axis} onchange={applyAxis} aria-label="Wheel axis">
				{#each data?.axes ?? [] as axisOption (axisOption.key)}
					<option value={axisOption.key} disabled={!axisOption.enabled} title={axisOption.reason ?? undefined}>
						{axisOption.label}{axisOption.enabled ? '' : ' (disabled)'}
					</option>
				{/each}
			</select>
		</label>
	</header>

	{#if error}
		<div
			class="error"
			role="alert"
			title="not implemented - see PARITY-TODO (library unavailable)"
		>
			Library wheel unavailable: {error}
		</div>
	{/if}

	{#if data && layout}
		{@const currentData = data}
		{#if !currentData.selected_axis_enabled}
			<p class="axis-note" title={currentData.selected_axis_reason ?? undefined}>
				{currentData.axes.find((a) => a.key === currentData.axis)?.label ?? currentData.axis} is disabled: {currentData.selected_axis_reason}
			</p>
		{/if}
		<div class="summary" aria-label="Library summary">
			<div title="Tracks in the library from state.db">
				<strong>{data.total_tracks}</strong><span>tracks</span>
			</div>
			<div title="Genre families with at least one classified track">
				<strong>{data.families.length}</strong><span>families</span>
			</div>
			<div
				title="Tracks whose genre matches no known family, or with no rekordbox mapping at all"
			>
				<strong>{data.unclassified_track_count}</strong><span>unclassified</span>
			</div>
		</div>

		{#if data.total_tracks === 0}
			<p class="empty" title="not implemented - see PARITY-TODO (empty library)">
				No tracks in this library.
			</p>
		{:else if data.families.length > 0}
			<svg
				class="wheel"
				viewBox={`0 0 ${VIEWBOX_SIZE} ${VIEWBOX_SIZE}`}
				role="img"
				aria-label="Radial library explorer"
			>
				<g transform={`translate(${CENTER} ${CENTER}) scale(${RADIUS_SCALE})`}>
					{#each layout.families as wedge (wedge.title)}
						<path
							d={wedgePath(wedge)}
							fill={wedge.color}
							fill-opacity="0.85"
							stroke="var(--surface)"
							stroke-width="0.004"
							vector-effect="non-scaling-stroke"
						><title>{wedgeTooltip(wedge)}</title></path>
					{/each}
					{#each layout.genres as wedge, i (i)}
						<path
							d={wedgePath(wedge)}
							fill={wedge.color}
							fill-opacity="0.6"
							stroke="var(--surface)"
							stroke-width="0.004"
							vector-effect="non-scaling-stroke"
						><title>{wedgeTooltip(wedge)}</title></path>
					{/each}
					{#each layout.tracks as wedge (wedge.stableId)}
						<path
							d={wedgePath(wedge)}
							fill={wedge.color}
							fill-opacity="0.4"
							stroke="var(--surface)"
							stroke-width="0.004"
							vector-effect="non-scaling-stroke"
						><title>{wedgeTooltip(wedge)}</title></path>
					{/each}
				</g>
			</svg>
		{/if}
	{:else if loading && !error}
		<p class="empty">Loading library...</p>
	{/if}
</section>

<style>
	.wheel-page { max-width: 1180px; margin: 0 auto; }
	header { display: flex; align-items: end; justify-content: space-between; gap: 1rem; margin-bottom: 1.3rem; }
	h2 { margin: 0.1rem 0 0.25rem; font-size: 1.75rem; letter-spacing: -0.03em; }
	.eyebrow { margin: 0; color: var(--accent); font-size: 0.68rem; font-weight: 800; letter-spacing: 0.16em; }
	.subtitle, .empty { margin: 0; color: var(--muted); }
	label { display: grid; gap: 0.3rem; color: var(--muted); font-size: 0.72rem; }
	label select { min-width: 170px; }
	.error { padding: 0.7rem 0.9rem; margin-bottom: 1rem; background: color-mix(in srgb, var(--danger) 15%, var(--surface)); border: 1px solid var(--danger); border-radius: 7px; }
	.axis-note { margin: 0 0 1rem; padding: 0.6rem 0.85rem; background: var(--surface); border: 1px solid var(--border); border-radius: 7px; color: var(--muted); font-size: 0.82rem; }
	.summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid var(--border); background: var(--surface); border-radius: 9px; margin-bottom: 1rem; }
	.summary div { display: grid; gap: 0.15rem; padding: 1rem 1.15rem; border-right: 1px solid var(--border); }
	.summary div:last-child { border-right: 0; }
	.summary strong { font-size: 1.45rem; font-variant-numeric: tabular-nums; }
	.summary span { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; }
	.wheel { display: block; width: 100%; max-width: 620px; margin: 0 auto; }
	@media (max-width: 900px) {
		.summary { grid-template-columns: 1fr; }
		.summary div { border-right: 0; border-bottom: 1px solid var(--border); }
		.summary div:last-child { border-bottom: 0; }
	}
</style>
