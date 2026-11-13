<script lang="ts">
	// PLAYS-9745: the History source (rekordbox History, mimicked). Loads the
	// nights over `GET /history`, one night over `GET /history/{day}`, saves a
	// night with `POST /history/{day}/playlist`. Titles come from the library
	// index the browser already holds. Agents reach every action through those
	// routes and `opendj history list|show|save-playlist`.
	import { onMount } from 'svelte';
	import ContextMenu from '../ContextMenu.svelte';
	import { bindMenuItems, type BoundMenuItem } from '$lib/context-menu/bind';
	import {
		loadMenuRegistry,
		menuRegistry,
		preloadMenuRegistry
	} from '$lib/context-menu/lazy.svelte';
	import { subscribeKind } from '$lib/api/events-bus';
	import { loadLibraryIndex } from '$lib/rb/library-index';
	import {
		fetchHistoryDay,
		fetchHistoryDays,
		saveHistoryDayAsPlaylist,
		type HistoryDay
	} from '$lib/history/history-api';
	import {
		historyPlayRows,
		historySavedMessage,
		keepOpenNight,
		type HistoryPlayView
	} from '$lib/history/history-view';
	import {
		historyMenuRunnersForDay,
		historyMenuRunnersForPlay
	} from '$lib/history/history-browser-menu';
	import { reportMenuLoadFailure } from './track-context-menu';
	import HistoryPanelView from './HistoryPanelView.svelte';

	let {
		oncollection,
		onsaved,
		onopennight,
		toast
	}: {
		oncollection: () => void;
		/** A night was opened: show its plays in the track table with Played at (rekordbox History). */
		onopennight: (date: string) => void;
		/** A night was saved: the new playlist's id, so the browser can open it. */
		onsaved: (playlistId: string) => void;
		toast: (message: string, level: 'info' | 'error') => void;
	} = $props();

	let days = $state<HistoryDay[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let night = $state<string | null>(null);
	let plays = $state<HistoryPlayView[]>([]);
	let playsLoading = $state(false);
	let playsError = $state<string | null>(null);
	let saving = $state(false);
	let menu = $state<{ x: number; y: number; items: () => BoundMenuItem[] } | null>(null);
	let nightSequence = 0;

	$effect(preloadMenuRegistry);

	async function loadDays(): Promise<void> {
		loading = true;
		try {
			days = (await fetchHistoryDays()).days;
			error = null;
			night = keepOpenNight(night, days);
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		} finally {
			loading = false;
		}
	}

	async function loadNight(date: string): Promise<void> {
		const sequence = ++nightSequence;
		playsLoading = true;
		playsError = null;
		try {
			const [day, index] = await Promise.all([fetchHistoryDay(date), loadLibraryIndex()]);
			if (sequence !== nightSequence) return;
			plays = historyPlayRows(day.plays, index.items);
		} catch (exc) {
			if (sequence !== nightSequence) return;
			playsError = exc instanceof Error ? exc.message : String(exc);
		} finally {
			if (sequence === nightSequence) playsLoading = false;
		}
	}

	function openNight(date: string): void {
		night = date;
		plays = [];
		void loadNight(date);
		onopennight(date);
	}

	function backToNights(): void {
		nightSequence += 1;
		night = null;
		plays = [];
		playsError = null;
		playsLoading = false;
	}

	async function saveNight(date: string): Promise<void> {
		saving = true;
		try {
			const saved = await saveHistoryDayAsPlaylist(date);
			toast(historySavedMessage(saved), 'info');
			onsaved(saved.playlist_id);
		} catch (exc) {
			toast(
				`Save as playlist failed: ${exc instanceof Error ? exc.message : String(exc)}`,
				'error'
			);
		} finally {
			saving = false;
		}
	}

	function openMenu(event: MouseEvent, items: () => BoundMenuItem[]): void {
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		event.stopPropagation();
		menu = { x: event.clientX, y: event.clientY, items };
	}

	function openDayMenu(event: MouseEvent, day: HistoryDay): void {
		openMenu(event, () =>
			bindMenuItems(
				menuRegistry()!.menuFor(
					{ kind: 'history-day', date: day.date },
					{ empty: day.count === 0 }
				),
				historyMenuRunnersForDay(day)
			)
		);
	}

	function openPlayMenu(event: MouseEvent, play: HistoryPlayView): void {
		const playId = play.play_id;
		openMenu(event, () =>
			bindMenuItems(
				menuRegistry()!.menuFor({ kind: 'history-row' }, {}),
				playId === null
					? {}
					: historyMenuRunnersForPlay({
							stable_id: play.stable_id,
							play_id: playId,
							title: play.title
						})
			)
		);
	}

	onMount(() => {
		void loadDays();
		// A play logged or deleted anywhere (deck load, the delete menus, the CLI) re-reads the list.
		return subscribeKind('plays', () => {
			void loadDays();
			if (night !== null) void loadNight(night);
		});
	});
</script>

<HistoryPanelView
	{days}
	{loading}
	{error}
	{night}
	{plays}
	{playsLoading}
	{playsError}
	{saving}
	{oncollection}
	onopen={openNight}
	onback={backToNights}
	onsave={(date) => void saveNight(date)}
	ondaymenu={openDayMenu}
	onplaymenu={openPlayMenu}
/>

{#if menu !== null && menuRegistry() !== null}
	<ContextMenu items={menu.items()} x={menu.x} y={menu.y} onclose={() => (menu = null)} />
{/if}
