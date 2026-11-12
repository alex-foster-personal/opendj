<!--
	ENRICH-01: the enrich-on-open card. One pill in the top bar, in flow so it
	covers nothing (issue #5748), that says what the library still lacks; a
	click expands the card below it, which asks the one opt-in question (stems).

	Every word comes from `$lib/enrich/enrich-card` (pure, unit tested) and
	every pixel from EnrichCardView.svelte (props only, one story per state);
	this component only fetches and wires buttons. Agent parity, one endpoint per
	control:
	  load      GET  /api/v1/enrich/summary          (enrich_cli summary)
	  Retry     POST /api/v1/ahead-analysis/retry    (ahead_analysis_cli retry)
	  Separate  POST /api/v1/jobs (via StemsPrompt)
	  Never     PUT  /api/v1/enrich/decisions/stems  (enrich_cli decide)
	  Ask again PUT  /api/v1/enrich/decisions/stems {answer: ask} (ENRICH-05)
	  Hide      session only, re-shown on the next open
	  More/Less view only; the card NEVER opens itself: needsAttention() flags
	            the pill ("needs you"), and only a click expands it. Esc and a
	            click outside close it.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { api, unwrap } from '$lib/api/rb-http';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import StemsPrompt from '$lib/components/rb/StemsPrompt.svelte';
	import EnrichCardView from '$lib/components/rb/EnrichCardView.svelte';
	import ArtworkSuggestionsDialogView from '$lib/components/rb/ArtworkSuggestionsDialogView.svelte';
	import { type EnrichSummary, cardVisible, needsAttention } from '$lib/enrich/enrich-card';
	import {
		type SuggestionsDoc,
		decideBody,
		shouldAutoOpen
	} from '$lib/enrich/artwork-suggestions';
	import { anyDeckPlaying } from '$lib/rb/playing-gate';

	const REFRESH_MS = 30_000;
	const HIDE_KEY = 'odj.enrich-card.hidden';
	const COVERS_SHOWN_KEY = 'odj.artwork-suggestions.shown';

	let summary = $state<EnrichSummary | null>(null);
	let loadError = $state<string | null>(null);
	let actionError = $state<string | null>(null);
	let hidden = $state(readFlag(HIDE_KEY));
	let asking = $state(false);
	let busy = $state(false);
	/** Only the user expands the card; the policy flags the pill instead. */
	let expanded = $state(false);
	/** LIBM-189: the store-cover review dialog. */
	let covers = $state<SuggestionsDoc | null>(null);
	let coversSelected = $state<Set<string>>(new Set());
	let coversError = $state<string | null>(null);
	let coversShown = $state(readFlag(COVERS_SHOWN_KEY));

	// Opens by itself once a session, never while a deck plays (shouldAutoOpen).
	$effect(() => {
		if (covers === null && shouldAutoOpen(summary?.artwork_suggestions ?? 0, anyDeckPlaying(), coversShown)) {
			coversShown = true;
			writeFlag(COVERS_SHOWN_KEY);
			void openCovers();
		}
	});

	function readFlag(key: string): boolean {
		try {
			return sessionStorage.getItem(key) === '1';
		} catch {
			return false;
		}
	}

	function writeFlag(key: string): void {
		try {
			sessionStorage.setItem(key, '1');
		} catch {
			// Storage refused (private window): the flag holds for this page only.
		}
	}

	async function openCovers(): Promise<void> {
		coversError = null;
		try {
			const doc = (await unwrap(api.GET('/api/v1/artwork/suggestions'))) as SuggestionsDoc;
			if (doc.pending.length === 0) return;
			covers = doc;
			coversSelected = new Set(doc.pending.map((s) => s.stable_id));
		} catch (error) {
			actionError = `GET /api/v1/artwork/suggestions failed: ${error instanceof Error ? error.message : String(error)}`;
		}
	}

	async function decideCovers(action: 'save-selected' | 'save-all' | 'skip'): Promise<void> {
		busy = true;
		coversError = null;
		try {
			const doc = (await unwrap(
				api.POST('/api/v1/artwork/suggestions/decide', { body: decideBody(action, coversSelected) })
			)) as SuggestionsDoc & { results: Record<string, { decision: string; reason?: string }> };
			const failed = Object.entries(doc.results).filter(([, r]) => r.decision === 'error');
			coversError = failed.length ? failed.map(([sid, r]) => `${sid}: ${r.reason}`).join('; ') : null;
			covers = doc.pending.length ? doc : null;
			coversSelected = new Set([...coversSelected].filter((sid) => doc.pending.some((s) => s.stable_id === sid)));
			await load();
		} catch (error) {
			coversError = error instanceof Error ? error.message : String(error);
		} finally {
			busy = false;
		}
	}

	function toggleCover(stableId: string): void {
		const next = new Set(coversSelected);
		if (next.has(stableId)) next.delete(stableId);
		else next.add(stableId);
		coversSelected = next;
	}

	const attention = $derived(needsAttention(summary, loadError, actionError));

	// ENRICH-05: a standing "never" keeps the pill up (collapsed, never flagged) so it can be undone, Hide included.
	const visible = $derived(cardVisible(summary, loadError, hidden));

	function hide(): void {
		hidden = true;
		expanded = false; // a pill a standing "never" keeps up stays, collapsed
		try {
			sessionStorage.setItem(HIDE_KEY, '1');
		} catch {
			// Storage refused (private window): the card hides for this page only.
		}
	}

	async function load(): Promise<void> {
		try {
			summary = (await unwrap(api.GET('/api/v1/enrich/summary'))) as EnrichSummary;
			loadError = null;
		} catch (error) {
			loadError = `GET /api/v1/enrich/summary failed: ${error instanceof Error ? error.message : String(error)}`;
		}
	}

	async function act(call: () => Promise<unknown>): Promise<void> {
		busy = true;
		actionError = null;
		try {
			await call();
			await load();
		} catch (error) {
			actionError = error instanceof Error ? error.message : String(error);
		} finally {
			busy = false;
		}
	}

	const retry = (): Promise<void> => act(() => unwrap(api.POST('/api/v1/ahead-analysis/retry')));
	const never = (): Promise<void> =>
		act(() =>
			unwrap(
				api.PUT('/api/v1/enrich/decisions/{lane}', {
					params: { path: { lane: 'stems' } },
					body: { answer: 'never' }
				})
			)
		);

	const askAgain = (): Promise<void> =>
		act(() =>
			unwrap(
				api.PUT('/api/v1/enrich/decisions/{lane}', {
					params: { path: { lane: 'stems' } },
					body: { answer: 'ask' }
				})
			)
		);

	onMount(() => {
		// LIBM-172: the summary is a multi-second engine read; it waits for the
		// boot library index (the scheduler's ceiling still runs it).
		bootScheduler.defer('enrich-card:load', () => void load());
		const timer = setInterval(() => void load(), REFRESH_MS);
		return () => clearInterval(timer);
	});
</script>

{#if visible}
	<EnrichCardView
		{summary}
		{loadError}
		{actionError}
		{busy}
		{asking}
		{expanded}
		{attention}
		ontoggle={() => (expanded = !expanded)}
		onclose={() => (expanded = false)}
		onhide={hide}
		onretry={retry}
		onask={() => (asking = true)}
		onnever={never}
		onaskagain={askAgain}
		onreviewcovers={() => void openCovers()}
	>
		{#snippet stemsPrompt()}
			<StemsPrompt onenqueued={() => void load()} onskip={() => (asking = false)} />
		{/snippet}
	</EnrichCardView>
{/if}
{#if covers}
	<ArtworkSuggestionsDialogView
		suggestions={covers.pending}
		note={covers.note}
		selected={coversSelected}
		{busy}
		error={coversError}
		ontoggle={toggleCover}
		onsaveselected={() => void decideCovers('save-selected')}
		onsaveall={() => void decideCovers('save-all')}
		onskip={() => void decideCovers('skip')}
		onclose={() => (covers = null)}
	/>
{/if}
