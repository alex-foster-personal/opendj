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
	  Hide      session only, re-shown on the next open
	  More/Less view only; the card NEVER opens itself: needsAttention() flags
	            the pill ("needs you"), and only a click expands it. Esc and a
	            click outside close it.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { api, unwrap } from '$lib/api/client';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import StemsPrompt from '$lib/components/rb/StemsPrompt.svelte';
	import EnrichCardView from '$lib/components/rb/EnrichCardView.svelte';
	import { type EnrichSummary, needsAttention } from '$lib/enrich/enrich-card';

	const REFRESH_MS = 30_000;
	const HIDE_KEY = 'odj.enrich-card.hidden';

	let summary = $state<EnrichSummary | null>(null);
	let loadError = $state<string | null>(null);
	let actionError = $state<string | null>(null);
	let hidden = $state(readHidden());
	let asking = $state(false);
	let busy = $state(false);
	/** Only the user expands the card; the policy flags the pill instead. */
	let expanded = $state(false);

	const attention = $derived(needsAttention(summary, loadError, actionError));

	const visible = $derived(!hidden && (loadError !== null || (summary?.show ?? false)));

	function readHidden(): boolean {
		try {
			return sessionStorage.getItem(HIDE_KEY) === '1';
		} catch {
			return false;
		}
	}

	function hide(): void {
		hidden = true;
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
	>
		{#snippet stemsPrompt()}
			<StemsPrompt onenqueued={() => void load()} onskip={() => (asking = false)} />
		{/snippet}
	</EnrichCardView>
{/if}
