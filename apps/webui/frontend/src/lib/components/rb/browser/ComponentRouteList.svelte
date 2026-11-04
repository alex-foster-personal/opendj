<script lang="ts">
	/**
	 * SET-6586 Routes view: a playlist's components grouped by slot, with a branch
	 * gutter that lights the chosen route. Swaps in for the track table while
	 * Routes mode is on (flag `sets.routes_view`, off by default).
	 * Design: https://claude.ai/artifact/Y4Ms8L8B3cLmK5qKgyFGzv
	 */
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import {
		createPlaylistSet,
		listPlaylistComponents,
		listPlaylistRoutes,
		updatePlaylistSetParts,
		type PlaylistComponent,
		type PlaylistRouteList
	} from '$lib/rb/api-playlist-sets';
	import type { BrowserRow } from './pane-contract.svelte';
	import {
		formatDuration,
		openingPick,
		matchSet,
		partsOf,
		pickFromParts,
		prunePick,
		sameParts,
		sharedTracks,
		slotGroups,
		tracksDuration
	} from './set-routes';
	import { setRoutesView } from './set-routes-view.svelte';

	let { playlistId, rows }: { playlistId: string; rows: readonly BrowserRow[] } = $props();

	const SLOT_H = 26;
	const HEAD_H = 30;
	const ROW_H = 26;
	const LANE_X0 = 10;
	const LANE_DX = 18;
	/** Routes per page: the table loads one page, then more on demand, never every route at once. */
	const ROUTE_PAGE_SIZE = 64;

	let components: PlaylistComponent[] = $state([]);
	let routeList = $state<PlaylistRouteList | null>(null);
	let loaded = $state(false);
	let error = $state<string | null>(null);
	let saveName = $state('');
	let busy = $state(false);
	let loadingMore = $state(false);

	const rowsById = $derived(new Map(rows.map((r) => [r.stable_id, r])));
	const groups = $derived(slotGroups(components));
	const byId = $derived(new Map(components.map((c) => [c.id, c])));
	const shared = $derived(sharedTracks(components));
	const pick = $derived(
		prunePick(setRoutesView.pick ?? openingPick(groups, components, setRoutesView.sets), components)
	);
	const parts = $derived(partsOf(pick, groups));
	const matched = $derived(matchSet(parts, setRoutesView.sets));
	const selectedSet = $derived(setRoutesView.sets.find((s) => s.id === setRoutesView.selectedSetId) ?? null);
	/** Parts of the selected set the components list does not hold (a deleted component); moving the set would drop them. */
	const hiddenParts = $derived(selectedSet ? selectedSet.parts.filter((id) => !byId.has(id)) : []);
	const routeTracks = $derived(parts.flatMap((id) => byId.get(id)?.stable_ids ?? []));
	const routeLen = $derived(tracksDuration(routeTracks, durationOf));
	/** Tracks that sit in two or more of the lit route's components, counted here so they do not depend on the loaded page. */
	const repeats = $derived.by(() => {
		const counts = new Map<string, number>();
		for (const id of parts) {
			for (const sid of new Set(byId.get(id)?.stable_ids ?? [])) counts.set(sid, (counts.get(sid) ?? 0) + 1);
		}
		return [...counts].filter(([, n]) => n > 1).map(([sid]) => sid);
	});

	function durationOf(sid: string): number | null {
		return rowsById.get(sid)?.duration_ms ?? null;
	}

	function slotTone(slot: string): string {
		if (slot === 'opener') return 'opener';
		if (slot === 'ending' || slot === 'encore') return 'ending';
		if (slot === 'peak') return 'peak';
		return 'body';
	}

	function lenTitle(len: { ms: number; unknown: number }, what: string): string {
		const base = `Total play time of ${what}, summed from each track's length`;
		return len.unknown > 0 ? `${base}; ${len.unknown} track(s) have no known length and are left out` : base;
	}

	function choose(slot: string, componentId: number): void {
		setRoutesView.pick = { ...pick, [slot]: componentId };
	}

	function chooseParts(next: readonly number[]): void {
		setRoutesView.pick = pickFromParts(next, components);
	}

	function errorText(exc: unknown): string {
		return exc instanceof Error ? exc.message : String(exc);
	}

	async function refresh(): Promise<void> {
		const id = playlistId;
		try {
			const [comps, listing] = await Promise.all([
				listPlaylistComponents(id),
				listPlaylistRoutes(id, ROUTE_PAGE_SIZE, 0)
			]);
			if (id !== playlistId) return;
			components = comps;
			routeList = listing;
			error = null;
		} catch (exc) {
			if (id === playlistId) error = errorText(exc);
		} finally {
			loaded = true;
		}
	}

	async function loadMoreRoutes(): Promise<void> {
		const id = playlistId;
		if (!routeList?.truncated || loadingMore) return;
		loadingMore = true;
		try {
			const page = await listPlaylistRoutes(id, ROUTE_PAGE_SIZE, routeList.routes.length);
			if (id !== playlistId || !routeList) return;
			routeList = { routes: [...routeList.routes, ...page.routes], total: page.total, truncated: page.truncated };
		} catch (exc) {
			if (id === playlistId) error = errorText(exc);
		} finally {
			loadingMore = false;
		}
	}

	async function saveAsSet(): Promise<void> {
		const name = saveName.trim();
		if (!name || busy || parts.length === 0) return;
		busy = true;
		try {
			const created = await createPlaylistSet(playlistId, name, parts);
			setRoutesView.sets = [...setRoutesView.sets, created];
			setRoutesView.selectedSetId = created.id;
			saveName = '';
			error = null;
			await refresh();
		} catch (exc) {
			error = errorText(exc);
		} finally {
			busy = false;
		}
	}

	async function moveSelectedSet(): Promise<void> {
		if (!selectedSet || busy || parts.length === 0 || hiddenParts.length > 0) return;
		busy = true;
		try {
			const moved = await updatePlaylistSetParts(playlistId, selectedSet.id, parts);
			setRoutesView.sets = setRoutesView.sets.map((s) => (s.id === moved.id ? moved : s));
			error = null;
			await refresh();
		} catch (exc) {
			error = errorText(exc);
		} finally {
			busy = false;
		}
	}

	/** Gutter width fits the slot with the most alternatives; 60px is the floor for one to three lanes. */
	const gutterW = $derived(
		Math.max(60, LANE_X0 * 2 + (Math.max(1, ...groups.map((g) => g.components.length)) - 1) * LANE_DX)
	);

	/** Gutter geometry: one lane per component in its slot, stacked like the rows. */
	const layout = $derived.by(() => {
		const nodes: { id: number; slotIndex: number; lane: number; top: number; bottom: number; on: boolean; tone: string }[] = [];
		let y = 0;
		groups.forEach((g, slotIndex) => {
			y += SLOT_H;
			g.components.forEach((c, lane) => {
				const top = y + HEAD_H / 2;
				y += HEAD_H + c.stable_ids.length * ROW_H;
				nodes.push({ id: c.id, slotIndex, lane, top, bottom: y - ROW_H / 2, on: pick[g.slot] === c.id, tone: slotTone(g.slot) });
			});
		});
		const edges: { d: string; on: boolean }[] = [];
		for (const a of nodes) {
			for (const b of nodes) {
				if (b.slotIndex !== a.slotIndex + 1) continue;
				const xa = LANE_X0 + a.lane * LANE_DX;
				const xb = LANE_X0 + b.lane * LANE_DX;
				const mid = (a.bottom + b.top) / 2;
				edges.push({ d: `M${xa} ${a.bottom} C ${xa} ${mid}, ${xb} ${mid}, ${xb} ${b.top}`, on: a.on && b.on });
			}
		}
		return { nodes, edges, height: y };
	});

	$effect(() => {
		const id = playlistId;
		// A different playlist must not keep this one's components or routes while its own load runs.
		components = [];
		routeList = null;
		error = null;
		loaded = false;
		const unkind = subscribeKind('playlist_components', (ids) => {
			if (ids.includes(id)) void refresh();
		});
		const unsets = subscribeKind('playlist_sets', (ids) => {
			if (ids.includes(id)) void refresh();
		});
		const unresync = subscribeResync(() => {
			void refresh();
		});
		void refresh();
		return () => {
			unkind();
			unsets();
			unresync();
		};
	});
</script>

<div class="route-view" data-testid="component-route-list">
	{#if error}
		<div class="route-error" role="alert" data-testid="route-error">{error}</div>
	{/if}
	{#if loaded && components.length === 0}
		<p class="route-empty" data-testid="route-empty">
			No components yet. Turn Routes off, select a run of tracks, then use Make component.
		</p>
	{:else if components.length > 0}
		<div class="route-bar" data-testid="route-bar">
			{#each parts as id, i (id)}
				{#if i > 0}<span class="arrow" aria-hidden="true">→</span>{/if}
				<span class="chip {slotTone(byId.get(id)?.slot ?? '')}">{byId.get(id)?.name}</span>
			{/each}
			{#if repeats.length > 0}
				<span
					class="repeat"
					data-testid="route-repeats"
					title="This route plays {repeats.length} track(s) more than once, because they sit in two of its components"
					>repeats {repeats.length}</span
				>
			{/if}
			<span class="match">
				<span title={lenTitle(routeLen, 'this route')}>{formatDuration(routeLen.ms)}</span>
				{#if matched}
					· set <b data-testid="route-matched-set">{matched.name} ({matched.play_count})</b>
				{:else}
					· unsaved route
				{/if}
			</span>
			{#if !matched}
				<input
					type="text"
					placeholder="Set name"
					bind:value={saveName}
					data-testid="route-save-name"
					onkeydown={(e) => {
						if (e.key === 'Enter') void saveAsSet();
					}}
				/>
				<button
					type="button"
					class="save"
					data-testid="route-save"
					disabled={busy || !saveName.trim()}
					onclick={() => void saveAsSet()}>Save as set</button
				>
				{#if selectedSet && selectedSet.parts.length > 0}
					<button
						type="button"
						data-testid="route-move-set"
						disabled={busy || hiddenParts.length > 0}
						title={hiddenParts.length > 0
							? `${selectedSet.name} uses a component from another playlist, which this view cannot show yet; moving it would drop that part`
							: `Change ${selectedSet.name}'s route to this one; its past runs keep the tracks they played`}
						onclick={() => void moveSelectedSet()}>Move {selectedSet.name} here</button
					>
				{/if}
			{/if}
		</div>

		<div class="routes" style:min-height="{layout.height}px" style:--gutter-w="{gutterW + 4}px">
			<svg class="gutter" aria-hidden="true" width={gutterW} height={layout.height} viewBox="0 0 {gutterW} {layout.height}">
				{#each layout.edges as edge, i (i)}
					<path d={edge.d} class="edge" class:on={edge.on} />
				{/each}
				{#each layout.nodes as node (node.id)}
					<path
						d="M{LANE_X0 + node.lane * LANE_DX} {node.top} L {LANE_X0 + node.lane * LANE_DX} {node.bottom}"
						class="lane {node.tone}"
						class:on={node.on}
					/>
					<circle
						cx={LANE_X0 + node.lane * LANE_DX}
						cy={node.top}
						r={node.on ? 5 : 3.5}
						class="node {node.tone}"
						class:on={node.on}
					/>
				{/each}
			</svg>
			<div class="rows">
				{#each groups as group (group.slot)}
					<div class="slot-head" style:height="{SLOT_H}px">
						<span class="label"
							>{group.slot}{#if group.components.length > 1}
								· <span title="Components in this slot; a route takes one of them">{group.components.length}</span> options{/if}</span
						>
					</div>
					{#each group.components as comp (comp.id)}
						{@const on = pick[group.slot] === comp.id}
						{@const len = tracksDuration(comp.stable_ids, durationOf)}
						<div
							class="comp-head"
							class:off={!on}
							style:height="{HEAD_H}px"
							role="button"
							tabindex="0"
							aria-pressed={on}
							aria-label="Use {comp.name} as the {group.slot}"
							data-testid="route-component"
							data-component-id={comp.id}
							onclick={() => choose(group.slot, comp.id)}
							onkeydown={(e) => {
								if (e.key === 'Enter' || e.key === ' ') {
									e.preventDefault();
									choose(group.slot, comp.id);
								}
							}}
						>
							<span class="name {slotTone(group.slot)}"
								>{comp.name}{#if comp.borrowed}<span
										class="borrowed"
										title="Borrowed from another playlist by a saved set">borrowed</span
									>{/if}</span
							>
							<span class="meta"
								><span title="Tracks in this component">{comp.stable_ids.length} tracks</span> ·
								<span title={lenTitle(len, 'this component')}>{formatDuration(len.ms)}</span></span
							>
							{#each comp.tags as tag (tag)}
								<span class="tag">#{tag}</span>
							{/each}
						</div>
						{#each comp.stable_ids as sid, i (`${sid}:${i}`)}
							{@const row = rowsById.get(sid)}
							<div class="row" class:off={!on} style:height="{ROW_H}px" data-testid="route-track">
								<div class="t">
									{#if row}
										{row.title ?? ''} <span class="artist">· {row.artist ?? ''}</span>
									{:else}
										<span class="artist" title="This track is not in the playlist any more">{sid.slice(0, 10)}</span>
									{/if}
									{#if shared.has(sid)}<span class="shared" title="This track is in more than one component">shared</span>{/if}
								</div>
								<div class="n" title="Tempo in beats per minute">{row?.bpm ?? ''}</div>
								<div class="n" title="Musical key (Camelot)">{row?.key ?? ''}</div>
								<div class="n" title="Track length">
									{row?.duration_ms != null ? formatDuration(row.duration_ms) : ''}
								</div>
							</div>
						{/each}
					{/each}
				{/each}
			</div>
		</div>

		{#if routeList}
			<div class="all-routes" data-testid="all-routes">
				<div class="all-head">
					<span class="count" title="Every way through this playlist's components, one per slot">{routeList.total}</span>
					<span class="label">possible routes</span>
					{#if routeList.truncated}
						<span class="label" title="Routes load a page at a time; the count is exact"
							>· showing {routeList.routes.length}</span
						>
					{/if}
				</div>
				<table>
					<thead><tr><th>Route</th><th class="n">Length</th><th>Saved as</th></tr></thead>
					<tbody>
						{#each routeList.routes as route (route.parts.join(','))}
							{@const saved = setRoutesView.sets.find((s) => s.id === route.set_id)}
							{@const len = tracksDuration(route.stable_ids, durationOf)}
							<tr
								class:hot={sameParts(route.parts, parts)}
								data-testid="all-routes-row"
								onclick={() => chooseParts(route.parts)}
							>
								<td>{route.parts.map((id) => byId.get(id)?.name ?? '?').join(' → ')}</td>
								<td class="n" title={lenTitle(len, 'this route')}>{formatDuration(len.ms)}</td>
								<td>{saved ? `${saved.name} (${saved.play_count})` : '-'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
				{#if routeList.truncated}
					<button type="button" data-testid="route-load-more" disabled={loadingMore} onclick={() => void loadMoreRoutes()}
						>Show more routes</button
					>
				{/if}
			</div>
		{/if}
	{/if}
</div>

<style>
	.route-view {
		flex: 1 1 auto;
		min-height: 0;
		overflow: auto;
		padding: 6px 10px 16px;
		background: var(--rb-panel);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-browser);
	}
	.route-error {
		color: var(--rb-red);
		padding: 4px 0;
	}
	.route-empty {
		color: var(--rb-text-dim);
		padding: 12px 0;
	}
	.label {
		font-size: var(--rb-fs-label);
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: var(--rb-text-dim);
	}
	.route-bar {
		display: flex;
		flex-wrap: wrap;
		gap: 6px 10px;
		align-items: center;
		padding: 6px 0 10px;
	}
	.chip {
		font-size: var(--rb-fs-label);
		font-weight: 600;
		padding: 2px 8px;
		border-radius: 3px;
		border: 1px solid currentColor;
	}
	.arrow {
		color: var(--rb-text-dim);
	}
	.repeat {
		font-size: var(--rb-fs-label);
		color: var(--rb-yellow);
	}
	.match {
		margin-left: auto;
		color: var(--rb-text-dim);
	}
	.match b {
		color: var(--rb-green);
	}
	.route-bar input {
		width: 9rem;
		font-size: var(--rb-fs-label);
	}
	.save {
		border-color: var(--rb-green);
		color: var(--rb-green);
	}
	.opener {
		color: var(--rb-accent);
	}
	.body {
		color: var(--rb-text-dim);
	}
	.ending {
		color: var(--rb-orange);
	}
	.peak {
		color: var(--rb-yellow);
	}
	.routes {
		position: relative;
		padding-left: var(--gutter-w, 64px);
		max-width: 760px;
	}
	.gutter {
		position: absolute;
		left: 0;
		top: 0;
	}
	.edge {
		fill: none;
		stroke: var(--rb-text-dim);
		stroke-width: 1.5;
		opacity: 0.28;
	}
	.edge.on {
		stroke: var(--rb-text);
		stroke-width: 2.5;
		opacity: 1;
	}
	.lane {
		fill: none;
		stroke: currentColor;
		stroke-width: 1.5;
		stroke-linecap: round;
		opacity: 0.28;
	}
	.lane.on {
		stroke-width: 4;
		opacity: 1;
	}
	.node {
		fill: var(--rb-panel);
		stroke: currentColor;
		stroke-width: 2;
		opacity: 0.5;
	}
	.node.on {
		fill: currentColor;
		opacity: 1;
	}
	.slot-head {
		display: flex;
		align-items: flex-end;
		padding-bottom: 3px;
		box-sizing: border-box;
	}
	.comp-head {
		display: flex;
		align-items: center;
		gap: 8px;
		cursor: pointer;
		border-radius: 4px;
		padding: 0 6px;
		margin-left: -6px;
		box-sizing: border-box;
	}
	.comp-head:hover {
		background: var(--rb-panel-raised);
	}
	.comp-head:focus-visible {
		outline: 2px solid var(--rb-accent);
		outline-offset: 2px;
	}
	.comp-head .name {
		font-weight: 700;
		font-size: calc(var(--rb-fs-browser) + 2px);
	}
	.comp-head .borrowed {
		margin-left: 6px;
		font-size: var(--rb-fs-label);
		font-weight: 400;
		opacity: 0.7;
	}
	.meta {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.tag {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		padding: 0 5px;
	}
	.row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 44px 36px 44px;
		gap: 8px;
		align-items: center;
		box-sizing: border-box;
	}
	.row .t {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.artist {
		color: var(--rb-text-dim);
	}
	.shared {
		font-size: 10px;
		color: var(--rb-text-dim);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		padding: 0 4px;
		margin-left: 6px;
	}
	.n {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.off {
		opacity: 0.22;
	}
	.comp-head,
	.row {
		transition: opacity 0.2s;
	}
	@media (prefers-reduced-motion: reduce) {
		.comp-head,
		.row {
			transition: none;
		}
	}
	.all-routes {
		margin-top: 16px;
		border-top: 1px solid var(--rb-border);
		padding-top: 8px;
	}
	.all-head {
		display: flex;
		align-items: baseline;
		gap: 8px;
	}
	.count {
		font-size: 28px;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
	}
	table {
		border-collapse: collapse;
		width: 100%;
		max-width: 824px;
	}
	th,
	td {
		text-align: left;
		padding: 4px 8px;
		border-bottom: 1px solid var(--rb-border);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	th {
		font-size: var(--rb-fs-label);
		text-transform: uppercase;
		letter-spacing: 0.1em;
		color: var(--rb-text-dim);
	}
	tbody tr {
		cursor: pointer;
	}
	tr.hot {
		background: color-mix(in srgb, var(--rb-panel) 75%, var(--rb-yellow) 25%);
	}
</style>
