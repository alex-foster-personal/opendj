<script lang="ts">
	/**
	 * /admin?tab=title-fix - fix tracks whose metadata is jammed into the title (TFIX-04).
	 *
	 * A scan only PROPOSES (raw -> proposed). Tracks change only when you select rows and
	 * press Apply, and every apply is one batch the Batches list can undo. Every button
	 * here has a CLI twin: python -m apps.webui.title_fix_cli <command>.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		POLICIES,
		TAG_FAMILIES,
		applyProposals,
		clearKey,
		fetchBatches,
		fetchConfig,
		fetchLatestScan,
		fetchProposals,
		fetchScan,
		fieldDiff,
		formatConfidence,
		formatUsd,
		rejectProposals,
		saveConfig,
		saveKey,
		scanAutoApplyWarning,
		scanProgressLabel,
		selectableIds,
		spendFraction,
		startScan,
		undoBatch,
		type ProposalQuery,
		type TitleFixBatch,
		type TitleFixConfig,
		type TitleFixOutcome,
		type TitleFixPage,
		type TitleFixScan
	} from './title-fix-api';
	import { applyWhileMounted, createScanPoller } from './title-fix-poller';

	const PAGE_SIZE = 100;
	const POLL_MS = 1500;

	let config = $state<TitleFixConfig | null>(null);
	let draftEnabled = $state(false);
	let draftPolicy = $state<string>('off');
	let draftCap = $state(0.5);
	let draftFamilies = $state<string[]>([...TAG_FAMILIES]);
	let keyInput = $state('');
	let scanLimit = $state<number | null>(null);
	let scan = $state<TitleFixScan | null>(null);
	let page = $state<TitleFixPage | null>(null);
	let batches = $state<TitleFixBatch[]>([]);
	let selected = $state<Set<string>>(new Set());
	let skipConflicts = $state(false);
	let query = $state<ProposalQuery>({
		state: 'pending',
		fixer_status: null,
		source: null,
		tag: null,
		limit: PAGE_SIZE,
		offset: 0
	});
	let error = $state<string | null>(null);
	let notice = $state<string | null>(null);
	let busy = $state(false);

	const scanRunning = $derived(scan?.state === 'running');
	const autoApplyWarning = $derived(
		config === null
			? null
			: scanAutoApplyWarning(
					// a running scan keeps the policy it started under, whatever the settings say now
					scanRunning && scan !== null && scan.auto_apply_policy ? scan.auto_apply_policy : config.auto_apply_policy
				)
	);
	const selection = $derived(page === null ? [] : selectableIds(page.items, selected));

	function message(exc: unknown): string {
		return exc instanceof Error ? exc.message : String(exc);
	}

	async function guarded(action: () => Promise<void>): Promise<void> {
		busy = true;
		error = null;
		try {
			await action();
		} catch (exc) {
			error = message(exc);
		} finally {
			busy = false;
		}
	}

	function adoptConfig(next: TitleFixConfig): void {
		config = next;
		draftEnabled = next.enabled;
		draftPolicy = next.auto_apply_policy;
		draftCap = next.spend_cap_usd;
		draftFamilies = [...next.tag_families];
	}

	async function loadProposals(): Promise<void> {
		page = await fetchProposals(query);
		selected = new Set();
	}

	async function loadAll(): Promise<void> {
		const steps: Array<() => Promise<boolean>> = [
			() => applyWhileMounted(poller.isStopped, fetchConfig, adoptConfig),
			() =>
				applyWhileMounted(poller.isStopped, fetchLatestScan, (latest) => {
					scan = latest;
				}),
			() =>
				applyWhileMounted(poller.isStopped, fetchBatches, (loaded) => {
					batches = loaded;
				}),
			() =>
				applyWhileMounted(poller.isStopped, () => fetchProposals(query), (loaded) => {
					page = loaded;
					selected = new Set();
				})
		];
		for (const step of steps) {
			if (!(await step())) return; // the panel was destroyed mid-load: no more requests, no more writes
		}
		if (scan?.state === 'running') schedulePoll();
	}

	const poller = createScanPoller<TitleFixScan>({
		pollMs: POLL_MS,
		fetchScan: () => (scan === null ? Promise.reject(new Error('no scan to poll')) : fetchScan(scan.scan_id)),
		onScan: (next) => {
			scan = next;
		},
		onSettled: () =>
			guarded(async () => {
				const loaded = await applyWhileMounted(poller.isStopped, fetchBatches, (latest) => {
					batches = latest;
				});
				if (!loaded) return; // the panel was destroyed while the batches loaded: no proposals request either
				await applyWhileMounted(poller.isStopped, () => fetchProposals(query), (latest) => {
					page = latest;
					selected = new Set();
				});
			}),
		onError: (exc) => {
			error = message(exc);
		}
	});

	function schedulePoll(): void {
		poller.schedule();
	}

	function toggleFamily(family: string, on: boolean): void {
		draftFamilies = on ? [...draftFamilies, family] : draftFamilies.filter((f) => f !== family);
	}

	function toggleRow(id: string, on: boolean): void {
		const next = new Set(selected);
		if (on) next.add(id);
		else next.delete(id);
		selected = next;
	}

	function toggleAll(on: boolean): void {
		selected = on && page !== null
			? new Set(page.items.filter((p) => p.state === 'pending').map((p) => p.proposal_id))
			: new Set();
	}

	function describeOutcome(verb: string, outcome: TitleFixOutcome): string {
		const skipped = outcome.skipped.map((s) => `${s.proposal_id.slice(0, 8)}: ${s.reason}`).join('; ');
		return (
			`${verb} ${outcome.applied.length}` +
			(skipped ? `, skipped ${outcome.skipped.length} (${skipped})` : '') +
			(outcome.warning ? `. Warning: ${outcome.warning}` : '')
		);
	}

	const onSaveConfig = () =>
		guarded(async () => {
			adoptConfig(
				await saveConfig({
					enabled: draftEnabled,
					auto_apply_policy: draftPolicy as TitleFixConfig['auto_apply_policy'],
					spend_cap_usd: draftCap,
					tag_families: draftFamilies as TitleFixConfig['tag_families']
				})
			);
			notice = 'config saved';
		});

	const onSaveKey = () =>
		guarded(async () => {
			const status = await saveKey(keyInput);
			keyInput = '';
			if (config !== null) config = { ...config, key: status };
			notice = 'key saved';
		});

	const onClearKey = () =>
		guarded(async () => {
			const status = await clearKey();
			if (config !== null) config = { ...config, key: status };
			notice = 'user key cleared';
		});

	const onScan = () =>
		guarded(async () => {
			scan = await startScan(scanLimit);
			notice = null;
			schedulePoll();
		});

	const onApply = () =>
		guarded(async () => {
			const outcome = await applyProposals(selection);
			notice = describeOutcome('applied', outcome);
			batches = await fetchBatches();
			await loadProposals();
		});

	const onReject = () =>
		guarded(async () => {
			notice = describeOutcome('rejected', await rejectProposals(selection));
			await loadProposals();
		});

	const onUndo = (batchId: string) =>
		guarded(async () => {
			notice = describeOutcome('undid', await undoBatch(batchId, skipConflicts));
			batches = await fetchBatches();
			await loadProposals();
		});

	const onFilter = () =>
		guarded(async () => {
			query = { ...query, offset: 0 };
			await loadProposals();
		});

	const onPage = (delta: number) =>
		guarded(async () => {
			query = { ...query, offset: Math.max(0, query.offset + delta * PAGE_SIZE) };
			await loadProposals();
		});

	onMount(() => void guarded(loadAll));
	onDestroy(() => poller.stop());
</script>

<section class="panel" id="title-fix">
	<h3>Fix broken track metadata</h3>
	<p class="sub">
		Finds tracks whose artist, key, version or junk text is jammed into the title. With
		auto-apply off, a scan only proposes and nothing changes until you select rows and Apply;
		with it on, a finished scan applies its eligible rows itself. Every apply can be undone
		below. CLI: <code>python -m apps.webui.title_fix_cli</code>.
	</p>

	{#if error !== null}
		<p class="warn" role="alert">{error}</p>
	{/if}
	{#if notice !== null}
		<p class="notice">{notice}</p>
	{/if}

	{#if config !== null}
		<fieldset class="config">
			<legend>Config</legend>
			<label title="When off, scans refuse to run (a scan spends OpenRouter credit)">
				<input type="checkbox" bind:checked={draftEnabled} /> Enabled
			</label>
			<label title="What may be applied WITHOUT selecting it. Review, project and not-music rows are never auto-applied.">
				Auto-apply
				<select bind:value={draftPolicy}>
					{#each POLICIES as policy (policy.value)}
						<option value={policy.value}>{policy.label}</option>
					{/each}
				</select>
			</label>
			<label title="A scan stops once its OpenRouter spend reaches this many US dollars (checked between chunks of rows)">
				Spend cap per scan (USD)
				<input type="number" min="0.01" max="100" step="0.01" bind:value={draftCap} />
			</label>
			<span class="families" title="Which tag families an apply may add to a track">
				Tags to write:
				{#each TAG_FAMILIES as family (family)}
					<label>
						<input
							type="checkbox"
							checked={draftFamilies.includes(family)}
							onchange={(e) => toggleFamily(family, e.currentTarget.checked)}
						/>
						{family}
					</label>
				{/each}
			</span>
			<button type="button" disabled={busy} onclick={onSaveConfig}>Save config</button>
		</fieldset>

		<fieldset class="config">
			<legend>OpenRouter key</legend>
			<span
				class="keystatus"
				title="Where the effective key comes from: a key saved here beats the engine's OPENROUTER_API_KEY environment variable. Only the last 4 characters are ever shown."
			>
				{#if config.key.configured}
					key: {config.key.source} ({config.key.hint})
				{:else}
					no key configured
				{/if}
			</span>
			<input
				type="password"
				autocomplete="off"
				placeholder="paste an OpenRouter key"
				bind:value={keyInput}
			/>
			<button type="button" disabled={busy || keyInput.trim() === ''} onclick={onSaveKey}>Save key</button>
			<button
				type="button"
				disabled={busy || config.key.source !== 'user'}
				title="Forget the key saved here (the environment key, if any, takes over)"
				onclick={onClearKey}>Clear saved key</button
			>
		</fieldset>
	{/if}

	<div class="scanrow">
		<label title="Scan only the first N tracks (by id). Empty scans the whole library.">
			Limit <input type="number" min="1" bind:value={scanLimit} placeholder="all" />
		</label>
		<button type="button" disabled={busy || scanRunning} onclick={onScan}>
			{scanRunning ? 'Scanning...' : 'Scan library'}
		</button>
		{#if autoApplyWarning !== null}
			<span class="warn" role="note">{autoApplyWarning}</span>
		{/if}
		{#if scan !== null}
			<span
				class="readout"
				title="Rows the fixer has processed out of the rows this scan requested"
			>{scanProgressLabel(scan)}</span>
			<span
				class="readout"
				title="OpenRouter spend so far for this scan, against its spend cap (US dollars)"
			>
				{formatUsd(scan.cost_usd)} / {formatUsd(scan.spend_cap_usd)}
				<meter min="0" max="1" value={spendFraction(scan)}></meter>
			</span>
			{#if scan.error}
				<span class="warn">{scan.error}</span>
			{/if}
		{/if}
	</div>

	<div class="filters">
		<label>
			State
			<select bind:value={query.state}>
				<option value="pending">pending</option>
				<option value="any">any</option>
				<option value="applied">applied</option>
				<option value="rejected">rejected</option>
				<option value="stale">stale</option>
				<option value="undone">undone</option>
				<option value="superseded">superseded</option>
				<option value="noop">noop</option>
			</select>
		</label>
		<label>
			Status
			<select bind:value={query.fixer_status}>
				<option value={null}>any</option>
				<option value="fixed">fixed</option>
				<option value="review">review</option>
				<option value="project">project</option>
				<option value="not_music">not_music</option>
				<option value="clean">clean</option>
				<option value="unknown">unknown</option>
			</select>
		</label>
		<label>Tag <input type="text" bind:value={query.tag} placeholder="remix" /></label>
		<label>Source <input type="text" bind:value={query.source} placeholder="regex+jev" /></label>
		<button type="button" disabled={busy} onclick={onFilter}>Filter</button>
	</div>

	{#if page !== null}
		<div class="actions">
			<button
				type="button"
				disabled={busy || selection.length === 0}
				onclick={onApply}
				title="Apply the {selection.length} selected pending proposal(s) to the library; the batch can be undone"
			>
				Apply selected ({selection.length})
			</button>
			<button type="button" disabled={busy || selection.length === 0} onclick={onReject}>
				Reject selected
			</button>
			<span class="readout" title="Proposals matching the filter, across all pages">{page.total} matching</span>
			<button type="button" disabled={busy || query.offset === 0} onclick={() => onPage(-1)}>Prev</button>
			<button
				type="button"
				disabled={busy || query.offset + PAGE_SIZE >= page.total}
				onclick={() => onPage(1)}>Next</button
			>
		</div>
		<table class="proposals">
			<thead>
				<tr>
					<th>
						<input
							type="checkbox"
							aria-label="select all pending rows on this page"
							onchange={(e) => toggleAll(e.currentTarget.checked)}
						/>
					</th>
					<th>Artist</th>
					<th>Title</th>
					<th>Tags</th>
					<th>Status</th>
					<th>Confidence</th>
					<th>Source</th>
					<th>State</th>
				</tr>
			</thead>
			<tbody>
				{#each page.items as row (row.proposal_id)}
					<tr title={[...row.notes, row.error].filter(Boolean).join('; ') || row.stable_id}>
						<td>
							<input
								type="checkbox"
								aria-label={`select ${row.original_title}`}
								disabled={row.state !== 'pending'}
								checked={selected.has(row.proposal_id)}
								onchange={(e) => toggleRow(row.proposal_id, e.currentTarget.checked)}
							/>
						</td>
						<td>{fieldDiff(row.original_artist, row.proposed_artist) ?? row.original_artist}</td>
						<td>{fieldDiff(row.original_title, row.proposed_title) ?? row.original_title}</td>
						<td>{row.tags.join(', ')}</td>
						<td>{row.fixer_status}</td>
						<td
							class="readout"
							title="JEV's probability that the regex proposal is correct (0 to 1); - when JEV was not asked"
						>{formatConfidence(row.confidence)}</td>
						<td>{row.source}</td>
						<td>{row.state}</td>
					</tr>
				{:else}
					<tr><td colspan="8" class="sub">No proposals match this filter.</td></tr>
				{/each}
			</tbody>
		</table>
	{/if}

	<h4>Batches</h4>
	<label title="Undo the rows that still match what was applied and leave the edited ones alone">
		<input type="checkbox" bind:checked={skipConflicts} /> Skip rows edited since apply
	</label>
	<ul class="batches">
		{#each batches as batch (batch.batch_id)}
			<li>
				<code>{batch.batch_id.slice(0, 8)}</code>
				{batch.kind} - <span class="readout" title="Proposals applied in this batch">{batch.proposal_count}</span>
				rows - {batch.created_at}
				{#if batch.undone_at === null}
					<button type="button" disabled={busy} onclick={() => onUndo(batch.batch_id)}>Undo</button>
				{:else}
					<span class="sub">undone {batch.undone_at}</span>
				{/if}
			</li>
		{:else}
			<li class="sub">No batches applied yet.</li>
		{/each}
	</ul>
</section>

<style>
	.panel {
		max-width: 1180px;
		margin-bottom: 1.5rem;
	}
	h3 {
		font-size: 1rem;
		color: var(--accent);
		margin: 0 0 0.25rem 0;
	}
	h4 {
		font-size: 0.9rem;
		margin: 1rem 0 0.25rem 0;
	}
	.sub {
		color: var(--muted);
		font-size: 0.85rem;
		margin: 0 0 0.5rem 0;
		max-width: 90ch;
	}
	.warn {
		color: var(--danger);
		font-size: 0.85rem;
	}
	.notice {
		font-size: 0.85rem;
	}
	code {
		background: var(--chip-bg);
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
	}
	.config {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		align-items: center;
		font-size: 0.85rem;
		margin-bottom: 0.5rem;
	}
	.families {
		display: inline-flex;
		gap: 0.5rem;
	}
	.scanrow,
	.filters,
	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		align-items: center;
		font-size: 0.85rem;
		margin: 0.5rem 0;
	}
	.readout {
		font-variant-numeric: tabular-nums;
	}
	.proposals {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.8rem;
	}
	.proposals th,
	.proposals td {
		text-align: left;
		padding: 0.2rem 0.4rem;
		border-bottom: 1px solid var(--chip-bg);
		vertical-align: top;
	}
	.batches {
		font-size: 0.85rem;
		padding-left: 1.25rem;
	}
</style>
