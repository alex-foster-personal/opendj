<script lang="ts">
	import { onDestroy } from 'svelte';
	import { getTrackAnalysisOrders, orderTrackAnalysis } from '$lib/rb/api-ingest';
	import { ANALYSIS_DOT_SLOTS, ANALYSIS_COLORS, ANALYSIS_ISSUE_COLORS, ANALYSIS_LABELS, analysisStatus, jobProgress, type AnalysisBadge, type AnalysisIssues, type AnalysisKind } from '$lib/rb/job-progress.svelte';
	let { stableId = '', badge = {}, issues = {}, mode = 'coverage', title }: { stableId?: string; badge?: AnalysisBadge; issues?: AnalysisIssues; mode?: 'coverage' | 'issues'; title?: string } = $props();
	const total = ANALYSIS_DOT_SLOTS.filter((kind) => kind !== null).length;
	const doneCount = $derived(ANALYSIS_DOT_SLOTS.filter((kind): kind is AnalysisKind => kind !== null && badge[kind] === true).length);
	const containerTitle = $derived(title ?? (mode === 'issues' ? 'data-quality issues' : `analysis coverage: ${doneCount}/${total}`));
	let open = $state(false);
	let closeTimer: ReturnType<typeof setTimeout> | null = null;
	function doneFor(kind: AnalysisKind): boolean { return mode === 'issues' ? issues[kind] !== undefined : badge[kind] === true; }
	function colorFor(kind: AnalysisKind): string { const issue = issues[kind]; return mode === 'issues' && issue !== undefined ? ANALYSIS_ISSUE_COLORS[issue.severity] : ANALYSIS_COLORS[kind]; }
	function statusFor(kind: AnalysisKind) { return analysisStatus(doneFor(kind), jobProgress.phaseFor(stableId, kind)); }
	function clearTimer(): void { if (closeTimer !== null) { clearTimeout(closeTimer); closeTimer = null; } }
	function openWithIntent(): void { clearTimer(); if (open) return; closeTimer = setTimeout(() => { open = true; closeTimer = null; void refresh(); }, 120); }
	function closeWithIntent(): void { clearTimer(); closeTimer = setTimeout(() => { open = false; closeTimer = null; }, 120); }
	function onKeydown(event: KeyboardEvent): void { if (event.key === 'Escape') { event.preventDefault(); open = false; } }
	async function refresh(): Promise<void> { for (const order of await getTrackAnalysisOrders(stableId)) jobProgress.upsert({ stable_id: stableId, kind: order.kind as AnalysisKind, phase: order.phase }); }
	async function order(kind: AnalysisKind, event: MouseEvent): Promise<void> { event.stopPropagation(); if (statusFor(kind) !== 'missing') return; jobProgress.upsert({ stable_id: stableId, kind, phase: 'queued' }); try { const ordered = await orderTrackAnalysis(stableId, kind); jobProgress.upsert({ stable_id: stableId, kind, phase: ordered.phase }); } catch (error) { jobProgress.clear(stableId, kind); throw error; } }
	onDestroy(clearTimer);
</script>

<span class="analysis-dots-wrap" role="presentation" onpointerenter={openWithIntent} onpointerleave={closeWithIntent} onkeydown={onKeydown}>
	<button class="analysis-dots" title={containerTitle} aria-label={containerTitle} aria-expanded={open} onclick={() => { open = !open; if (open) void refresh(); }}>
		{#each ANALYSIS_DOT_SLOTS as kind, i (i)}
			{#if kind === null}<span class="dot empty" aria-hidden="true"></span>{:else}<span class="dot" class:on={doneFor(kind)} style={`--dot:${colorFor(kind)}`} data-kind={kind} aria-hidden="true"></span>{/if}
		{/each}
	</button>
	{#if open}
		<!-- Non-modal: the hover trigger stays usable, so a focus trap would block table navigation. -->
		<div class="analysis-popover" role="dialog" tabindex="-1" aria-label={`${containerTitle} details`} onpointerenter={openWithIntent} onpointerleave={closeWithIntent}>
			{#each ANALYSIS_DOT_SLOTS as kind (kind)}{#if kind !== null}
				{@const status = statusFor(kind)}
				<button class="analysis-row" class:orderable={status === 'missing'} disabled={status !== 'missing'} onclick={(event) => order(kind, event)}><span class="row-dot" style={`--dot:${colorFor(kind)}`}></span><span>{ANALYSIS_LABELS[kind]}</span><span>{status}</span></button>
			{/if}{/each}
		</div>
	{/if}
</span>

<style>
.analysis-dots-wrap { position: relative; display: inline-block; }.analysis-dots { display:grid;grid-template-columns:repeat(3,4px);grid-template-rows:repeat(3,4px);gap:1px;width:14px;height:14px;padding:0;border:0;background:transparent;cursor:pointer; }.dot { width:4px;height:4px;border-radius:.5px;background:color-mix(in srgb,var(--rb-text,#888) 18%,transparent); }.dot.on,.row-dot { background:var(--dot); }.dot.empty { background:transparent; }.analysis-popover { position:absolute;z-index:20;left:0;bottom:calc(100% + 6px);min-width:170px;padding:4px;border:1px solid var(--rb-border,#47505d);background:var(--rb-bg,#141820); }.analysis-row { display:grid;grid-template-columns:10px 1fr auto;gap:5px;width:100%;padding:3px 4px;border:0;background:transparent;color:var(--rb-text,#ddd);text-align:left;font:inherit;font-size:10px; }.analysis-row.orderable { cursor:pointer; }.analysis-row.orderable:hover,.analysis-row.orderable:focus-visible { background:color-mix(in srgb,var(--rb-accent,#6ec8ff) 20%,transparent); }.row-dot { width:7px;height:7px;margin-top:3px;border-radius:50%; }
</style>
