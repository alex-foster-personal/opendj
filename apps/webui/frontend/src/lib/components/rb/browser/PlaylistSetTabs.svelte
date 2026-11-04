<script lang="ts">
	import { untrack } from 'svelte';
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import {
		createPlaylistSet,
		createPlaylistSetRun,
		listPlaylistSets,
		type PlaylistSet
	} from '$lib/rb/api-playlist-sets';
	import { pushToast } from '$lib/stores.svelte';
	import { setRoutesViewEnabled } from '$lib/sets/routes-view-flag';
	import type { BrowserRow } from './pane-contract.svelte';
	import PlaylistSetTabsView from './PlaylistSetTabsView.svelte';
	import { routeSetToFollow } from './playlist-set-tabs';
	import { setRoutesView } from './set-routes-view.svelte';

	/** SET-6586 tools load on first use, only with the flag on (bundle budget). */
	const loadSetRoutesTools = () => import('./SetRoutesTools.svelte');
	/** The route helpers ship in a lazy chunk, so flag-off users never download them (bundle budget). */
	const loadSetRoutes = () => import('./set-routes');

	let {
		playlistId,
		rows = [],
		selectedOrders = []
	}: { playlistId: string; rows?: readonly BrowserRow[]; selectedOrders?: readonly number[] } = $props();

	let sets: PlaylistSet[] = $state([]);
	let selectedId = $state<number | null>(null);
	let mode: 'practice' | 'performance' = $state('practice');
	let newName = $state('');
	let creating = $state(false);
	let running = $state(false);
	let activeKind = $state<'practice' | 'performance' | null>(null);
	let activeSetId = $state<number | null>(null);
	let runNote = $state<string | null>(null);
	let runError = $state(false);

	function kindName(kind: 'practice' | 'performance'): string {
		return kind === 'practice' ? 'Practice' : 'Performance';
	}

	function runIsLive(kind: 'practice' | 'performance'): boolean {
		return activeKind === kind && activeSetId === selectedId && !runError;
	}

	function startLabel(kind: 'practice' | 'performance'): string {
		if (running && activeKind === kind) return 'Starting...';
		if (runIsLive(kind)) return `${kindName(kind)} on`;
		return kind === 'practice' ? 'Start practice' : 'Start performance';
	}

	function patchPlayCount(setId: number, playCount: number): void {
		sets = sets.map((set) => (set.id === setId ? { ...set, play_count: playCount } : set));
	}

	// SET-6586 Routes view (flag `sets.routes_view`, off by default).
	const routesOn = $derived(setRoutesViewEnabled());

	function glyphText(set: PlaylistSet): string | null {
		if (!routesOn || set.parts.length === 0) return null;
		return set.parts.map((id) => setRoutesView.glyphs.get(id) ?? '?').join('·');
	}

	function selectSet(set: PlaylistSet): void {
		if (!routesOn) return;
		setRoutesView.selectedSetId = set.id;
		if (set.parts.length > 0) void pickFor(set);
	}

	async function pickFor(set: PlaylistSet): Promise<void> {
		const { pickFromParts } = await loadSetRoutes();
		if (selectedId !== set.id) return;
		// Empty until the components load: null lets the view fall back to the opening route.
		const next = pickFromParts(set.parts, setRoutesView.components);
		setRoutesView.pick = Object.keys(next).length > 0 ? next : null;
		setRoutesView.pickPending = setRoutesView.pick === null;
	}

	async function refresh(): Promise<void> {
		const id = playlistId;
		const next = await listPlaylistSets(id);
		if (id !== playlistId) return;
		const before = sets.find((s) => s.id === selectedId);
		sets = next;
		if (setRoutesView.playlistId === id) setRoutesView.sets = sets;
		const after = sets.find((s) => s.id === selectedId);
		if (routesOn && before && after) void repickIfChanged(before, after);
		if (selectedId !== null && !sets.some((s) => s.id === selectedId)) {
			selectedId = null;
		}
	}

	// Another client changed the selected set's parts: rebuild the lit route from the saved set, so
	// "Move <set> here" cannot overwrite that change with the stale pick.
	async function repickIfChanged(before: PlaylistSet, after: PlaylistSet): Promise<void> {
		const { sameParts } = await loadSetRoutes();
		if (!sameParts(before.parts, after.parts)) {
			if (after.parts.length > 0) await pickFor(after);
			else {
				setRoutesView.pick = null;
				setRoutesView.pickPending = false;
			}
		}
	}

	async function handleCreate(): Promise<void> {
		const name = newName.trim();
		if (!name || creating) return;
		creating = true;
		try {
			const created = await createPlaylistSet(playlistId, name);
			newName = '';
			await refresh();
			selectedId = created.id;
			// The route side follows the new set too, or the sync effect would pull the old one back.
			setRoutesView.selectedSetId = created.id;
		} finally {
			creating = false;
		}
	}

	async function handleRun(kind: 'practice' | 'performance'): Promise<void> {
		if (selectedId === null || running) return;
		const setId = selectedId;
		const previous = sets.find((set) => set.id === setId)?.play_count ?? 0;
		const name = kindName(kind);
		running = true;
		mode = kind;
		activeKind = kind;
		activeSetId = setId;
		runError = false;
		runNote = `Starting ${kind}...`;
		patchPlayCount(setId, previous + 1);
		try {
			const result = await createPlaylistSetRun(playlistId, setId, kind);
			try {
				await refresh();
			} catch {
				// The run is already recorded. A failed list reload must not hide it.
			}
			patchPlayCount(setId, result.play_count);
			runError = false;
			runNote = `${name} running. Play count ${result.play_count}.`;
			pushToast(runNote, 'info');
		} catch (error) {
			patchPlayCount(setId, previous);
			runError = true;
			const message =
				error instanceof Error && error.message.trim() !== ''
					? error.message
					: 'the set run did not start';
			runNote = `Could not start ${kind}: ${message}`;
			pushToast(runNote, 'error');
		} finally {
			running = false;
		}
	}

	// Save as set in the route list selects the new set there. The tabs follow, so Start practice and
	// Start performance run the set the route shows, not the one that was selected before.
	// Reads only the route side, so a tab choice made here never re-runs this and gets pulled back.
	let lastRouteSetId: number | null = null;
	$effect(() => {
		const route = routesOn ? setRoutesView.selectedSetId : null;
		const follow = untrack(() =>
			routeSetToFollow(route, lastRouteSetId, selectedId, sets.map((s) => s.id))
		);
		lastRouteSetId = route;
		if (follow !== null) selectedId = follow;
	});

	$effect(() => {
		const id = playlistId;
		selectedId = null;
		setRoutesView.show(id);
		const unkind = subscribeKind('playlist_sets', (ids) => {
			if (ids.includes(id)) {
				void refresh();
			}
		});
		const unresync = subscribeResync(() => {
			void refresh();
		});
		void refresh();
		return () => {
			unkind();
			unresync();
		};
	});
</script>

{#snippet routesTools()}
	{#if routesOn}
		{#await loadSetRoutesTools() then tools}
			<tools.default {playlistId} {rows} {selectedOrders} {sets} />
		{/await}
	{/if}
{/snippet}

<PlaylistSetTabsView
	{sets}
	routeLitId={routesOn ? setRoutesView.routeSetId : null}
	glyphFor={glyphText}
	onselect={selectSet}
	extra={routesTools}
	bind:selectedId
	bind:mode
	bind:newName
	{creating}
	{running}
	runNote={activeSetId === selectedId ? runNote : null}
	{runError}
	{runIsLive}
	{startLabel}
	oncreate={() => void handleCreate()}
	onrun={(kind) => void handleRun(kind)}
/>
