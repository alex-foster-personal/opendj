<script lang="ts">
	// #9467: root-mounted Cmd K v2 bar; lazy-loaded from +layout.svelte.
	import { onMount } from 'svelte';
	import CommandBarV2 from '$lib/components/rb/CommandBarV2.svelte';
	import { cmdkBrowser } from '$lib/rb/cmdk-browser-source.svelte';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import type { DeckId } from '$lib/rb/deck-id';
	import type { LoadGuard, SettleMeta } from '$lib/rb/cmdk-load';
	import {
		deckCommandDispatchCount,
		deckFaderWriteCount,
		deckLoadDispatchCount,
		performanceCommandStatus,
		pushToast,
		registerCmdkAdapter,
		runCoordinatedFromUi,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import {
		noteCmdkCommandPick,
		setAvailableOfflineFilter,
		setHideBrokenLinks,
		setNextOnlyFilter,
		setRemixesFilter,
		setVocalsFilter,
		uiPrefs
	} from '$lib/rb/prefs.svelte';

	const panel = $derived(cmdkBrowser.panel);
	const tracks = $derived(panel !== null);

	function livePanel() {
		const p = cmdkBrowser.panel;
		if (p === null) throw new Error('No library browser on this page');
		return p;
	}

	function openPlaylist(list: PlaylistNode): void {
		cmdkBrowser.panel?.commandBarOpenPlaylist(list);
	}

	function commandBarLoad(
		row: Parameters<NonNullable<typeof cmdkBrowser.panel>['commandBarV2Load']>[0],
		deck: DeckId,
		stillNewest?: LoadGuard
	): Promise<void> {
		return livePanel().commandBarV2Load(row, deck, stillNewest);
	}

	function fetchMeta(stable_id: string): Promise<SettleMeta> {
		return livePanel().commandBarV2Meta(stable_id);
	}

	function reservation(deck: DeckId): number {
		return cmdkBrowser.panel?.commandBarV2Reservation(deck) ?? 0;
	}

	function soloVocals(
		deck: DeckId,
		settled: 'ready' | 'unavailable' | 'error' | 'timeout'
	): Promise<void> {
		return cmdkBrowser.panel?.commandBarSoloVocals(deck, settled) ?? Promise.resolve();
	}

	onMount(() => {
		cmdkBrowser.hostMounted = true;
		return () => {
			cmdkBrowser.hostMounted = false;
		};
	});
</script>

<CommandBarV2
	{tracks}
	rows={panel?.commandBarRows() ?? []}
	playlistTitle={panel?.commandBarTitle() ?? ''}
	hideBroken={panel?.commandBarHideBroken() ?? false}
	playlists={panel?.commandBarPlaylists() ?? []}
	{openPlaylist}
	load={commandBarLoad}
	{fetchMeta}
	{reservation}
	{soloVocals}
	{uiPrefs}
	{setAvailableOfflineFilter}
	{setHideBrokenLinks}
	{setNextOnlyFilter}
	{setRemixesFilter}
	{setVocalsFilter}
	runCommand={(command, source) =>
		runPerformanceCommandFromUi(command, performance.now(), undefined, source)}
	runCoordinated={runCoordinatedFromUi}
	deckLoadDispatches={deckLoadDispatchCount}
	faderWrites={deckFaderWriteCount}
	deckCommands={deckCommandDispatchCount}
	deckPending={(deck) => performanceCommandStatus.deck_pending[deck]}
	registerAdapter={registerCmdkAdapter}
	aiToast={(message) => pushToast(message, 'error')}
	noteCommandPick={noteCmdkCommandPick}
/>
