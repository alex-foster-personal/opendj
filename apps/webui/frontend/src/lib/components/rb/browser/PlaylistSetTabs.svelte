<script lang="ts">
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import {
		createPlaylistSet,
		createPlaylistSetRun,
		listPlaylistSets,
		type PlaylistSet
	} from '$lib/rb/api-playlist-sets';
	import { pushToast } from '$lib/stores.svelte';
	import PlaylistSetTabsView from './PlaylistSetTabsView.svelte';

	let { playlistId }: { playlistId: string } = $props();

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

	async function refresh(): Promise<void> {
		sets = await listPlaylistSets(playlistId);
		if (selectedId !== null && !sets.some((s) => s.id === selectedId)) {
			selectedId = null;
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

	$effect(() => {
		const id = playlistId;
		selectedId = null;
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

<PlaylistSetTabsView
	{sets}
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
