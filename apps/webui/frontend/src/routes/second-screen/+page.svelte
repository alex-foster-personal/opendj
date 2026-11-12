<script lang="ts">
	// SECONDSCREEN-11713: a library-only window for a second monitor. It never
	// creates audio: the /performance window owns the engine, and this page
	// only reads the ui-mirror and posts load orders to the shared order bus
	// (the same path the CLI and agents use). Preview is off here because the
	// preview player would play to the system default output, which during a
	// set is the controller's master.
	import '$lib/rb/theme.css';
	import { onDestroy, onMount } from 'svelte';
	import BrowserPanel from '$lib/components/rb/BrowserPanel.svelte';
	import { API_BASE } from '$lib/api/base';
	import { pushToast } from '$lib/stores.svelte';
	import {
		DECK_NUMBERS,
		buildLoadOrder,
		deckIsEmpty,
		describeLoadResult,
		nextFreeDeck,
		remainingLabel,
		type DeckNumber,
		type MirrorDecks
	} from '$lib/library/second-screen-load';

	const MIRROR_POLL_MS = 1000;

	let decks = $state<MirrorDecks>({});
	let mainOpen = $state<boolean | null>(null);
	let mirrorError = $state<string | null>(null);
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	async function refreshMirror(): Promise<void> {
		try {
			const res = await fetch(`${API_BASE}/api/v1/state/ui-mirror`);
			// 409 is the engine saying no performance page has published yet.
			if (res.status === 409) {
				decks = {};
				mainOpen = false;
				mirrorError = null;
				return;
			}
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const body = (await res.json()) as { decks?: MirrorDecks; mirror_stale?: boolean };
			decks = body.decks ?? {};
			mainOpen = body.mirror_stale !== true;
			mirrorError = null;
		} catch (exc) {
			mirrorError = exc instanceof Error ? exc.message : String(exc);
		}
	}

	async function postLoad(stableId: string, deck: DeckNumber): Promise<void> {
		let status: number;
		let body: unknown = null;
		try {
			const res = await fetch(`${API_BASE}/api/v1/commands`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(buildLoadOrder(deck, stableId))
			});
			status = res.status;
			const text = await res.text();
			body = text === '' ? null : JSON.parse(text);
		} catch (exc) {
			pushToast(`Deck ${deck}: could not reach the engine`, 'error', undefined, exc);
			return;
		}
		const outcome = describeLoadResult(deck, status, body);
		pushToast(outcome.message, outcome.ok ? 'info' : 'error');
		void refreshMirror();
	}

	const library = {
		publish(): void {},
		preview(): void {
			pushToast(
				'Preview is off here (it would play through the master). To load, hover a title and pick deck 1-4.',
				'warn'
			);
		},
		async loadToDeck(row: { stable_id: string }, deck: DeckNumber | null): Promise<void> {
			const target = deck ?? nextFreeDeck(decks);
			if (target === null) {
				pushToast('Every deck holds a track: pick a deck to load onto.', 'error');
				return;
			}
			await postLoad(row.stable_id, target);
		}
	};

	onMount(() => {
		void refreshMirror();
		pollTimer = setInterval(() => void refreshMirror(), MIRROR_POLL_MS);
	});
	onDestroy(() => {
		if (pollTimer !== null) clearInterval(pollTimer);
	});
</script>

<svelte:head><title>Open DJ - Library (second screen)</title></svelte:head>

<div class="perf-root second-screen" data-testid="second-screen">
	<header class="ss-decks" aria-label="Decks">
		{#each DECK_NUMBERS as n (n)}
			{@const d = decks[String(n)]}
			<div class="ss-deck" class:playing={d?.playing === true} data-testid="ss-deck-{n}">
				<span class="ss-num">{n}</span>
				{#if mainOpen !== true || mirrorError !== null}
					<span class="ss-empty">unavailable</span>
				{:else if deckIsEmpty(d)}
					<span class="ss-empty">empty</span>
				{:else}
					<span class="ss-title" title={`${d?.artist ?? ''} - ${d?.title ?? ''}`}
						>{d?.title ?? '(untitled)'}</span
					>
					<span class="ss-meta">
						{#if typeof d?.effective_bpm === 'number'}<span
								title="Effective tempo in beats per minute">{d.effective_bpm.toFixed(1)}</span
							>{/if}
						{#if d?.key}<span title="Musical key (Camelot)">{d.key}</span>{/if}
						{#if remainingLabel(d)}<span title="Time left in the track (m:ss)"
								>{remainingLabel(d)}</span
							>{/if}
					</span>
				{/if}
			</div>
		{/each}
		{#if mirrorError !== null}
			<span class="ss-warn" role="status">Engine unreachable: {mirrorError}</span>
		{:else if mainOpen === false}
			<span class="ss-warn" role="status"
				>Main window unavailable: open or focus the performance window</span
			>
		{/if}
	</header>
	<BrowserPanel {library} />
</div>

<style>
	/* Same shell as Crate Desk: a fixed grid whose last row gives the browser
	 * (grid-area: browser) every pixel the deck strip does not use. */
	.second-screen {
		position: fixed;
		inset: 0;
		display: grid;
		grid-template-areas:
			'decks'
			'browser';
		grid-template-rows: auto minmax(0, 1fr);
		background: var(--rb-bg);
	}
	.ss-decks {
		grid-area: decks;
		display: flex;
		gap: 6px;
		padding: 4px 6px;
		align-items: center;
		font-size: 12px;
	}
	.ss-deck {
		display: flex;
		gap: 6px;
		align-items: center;
		flex: 1 1 0;
		min-width: 0;
		padding: 3px 6px;
		border-radius: 4px;
		background: rgba(255, 255, 255, 0.05);
	}
	.ss-deck.playing {
		box-shadow: inset 0 -2px 0 #2fd27a;
	}
	.ss-num {
		font-weight: 700;
		opacity: 0.7;
	}
	.ss-title {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ss-meta {
		display: flex;
		gap: 6px;
		opacity: 0.75;
		font-variant-numeric: tabular-nums;
	}
	.ss-empty {
		opacity: 0.4;
	}
	.ss-warn {
		color: #ffb347;
		white-space: nowrap;
	}
</style>
