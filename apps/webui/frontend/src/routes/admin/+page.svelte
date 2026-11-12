<script lang="ts">
	/**
	 * /admin - operator panel. Two data sections on the KPI tab: the demucs
	 * farm KPI ledger (the durable home for what used to be the standalone
	 * (private path)), and the code-quality ratchet the merge
	 * gate already enforces on every commit. Both are read-only views of a
	 * file the repo already maintains for its own reasons; this page is just
	 * where a human (or a hiring lead evaluating the engineering practice)
	 * can see them without a checkout.
	 *
	 * Farm data comes from GET /api/v1/bench/kpi, ratchet data from
	 * GET /api/v1/admin/quality-ratchet, never from either file directly, so
	 * an agent can curl exactly what is rendered here. Errors are loud: a
	 * daemon that cannot serve either file renders a banner, not an empty
	 * grid.
	 *
	 * TABS. Seven in-page tabs share one ARIA tablist: "KPI ledger" (default,
	 * demucs farm + perf KPIs), "Lyrics generator" at ?tab=lyrics, "Quality
	 * ratchet" at ?tab=quality, "Diagnostics" at ?tab=diagnostics, "Playground"
	 * at ?tab=playground, "Spotify" at ?tab=spotify, and "Title fixes" at
	 * ?tab=title-fix. A Setup header action beside the strip leaves for /setup
	 * through the shared entry point so the doors into the wizard behave
	 * identically.
	 *
	 * Non-default panels import with import() (#11467) so those
	 * modules are not in this route node's static closure until the tab opens.
	 */
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { RUN_SETUP_TITLE, runSetup, runSetupBlocked } from '$lib/setup/run-setup';
	import PreflightScreen from '$lib/components/preflight/PreflightScreen.svelte';
	import KpiTile from './KpiTile.svelte';
	import FarmJobsPanel from './FarmJobsPanel.svelte';
	import KpiTable from './KpiTable.svelte';
	import LyricJobs from './LyricJobs.svelte';
	import LyricSourceOrder from './LyricSourceOrder.svelte';
	import LyricTriage from './LyricTriage.svelte';
	import ReviewPanel from './ReviewPanel.svelte';
	import LyricsKpiPanel from './LyricsKpiPanel.svelte';
	import EntitlementsInspector from './EntitlementsInspector.svelte';
	import QualityRatchet from './QualityRatchet.svelte';
	import RunNotes from './RunNotes.svelte';
	import TipLayer from './TipLayer.svelte';
	import StatusOverview from './StatusOverview.svelte';
	import { backstageEnabled } from '$lib/shell/backstage-flag';
	import { adminPanelFromUrl, adminTabFromUrl, ADMIN_PANEL_IDS, type AdminTab } from './admin-tab';
	import {
		fetchKpiLedger,
		fetchPerfKpiLedger,
		type KpiLedger,
		type PerfKpiLedger
	} from './kpi-api';

	let ledger = $state<KpiLedger | null>(null);
	let error = $state<string | null>(null);
	let perf = $state<PerfKpiLedger | null>(null);
	let perfError = $state<string | null>(null);

	// ----- tabs --------------------------------------------------------------
	const tab = $derived(adminTabFromUrl($page.url));
	/** Backstage (shell.backstage ON only): ?p=<id> shows exactly one panel and
	 * hides the tab strip. With the flag off ?p is ignored, as before. */
	const panel = $derived(
		backstageEnabled() ? adminPanelFromUrl($page.url) : ({ kind: 'none' } as const)
	);

	function selectTab(next: AdminTab): void {
		const url = new URL($page.url);
		if (next === 'kpi') {
			url.searchParams.delete('tab');
		} else {
			url.searchParams.set('tab', next);
		}
		void goto(url, { replaceState: true, keepFocus: true, noScroll: true });
	}

	let setupBusy = $state(false);
	let setupError = $state<string | null>(null);
	/** Only a FINAL refusal disables the Setup action; an unfinished health probe
	 * is resolved by the click itself. */
	const setupBlocked = $derived(runSetupBlocked());

	async function onSetupTab(): Promise<void> {
		if (setupBusy) return;
		setupBusy = true;
		setupError = null;
		try {
			setupError = await runSetup(goto, $page.url.pathname);
		} finally {
			setupBusy = false;
		}
	}

	const metrics = $derived(ledger ? Object.entries(ledger.kpis) : []);
	const latestRun = $derived(ledger ? ledger.snapshots[ledger.snapshots.length - 1] : null);

	const perfMetrics = $derived(perf ? Object.entries(perf.kpis) : []);

	// Independent loads: one ledger failing must not blank the other's cards.
	onMount(() => {
		fetchKpiLedger().then(
			(loaded) => (ledger = loaded),
			(exc) => (error = exc instanceof Error ? exc.message : String(exc))
		);
		fetchPerfKpiLedger().then(
			(loaded) => (perf = loaded),
			(exc) => (perfError = exc instanceof Error ? exc.message : String(exc))
		);
	});
</script>

{#if panel.kind === 'panel'}
<h2>Backstage</h2>
<div class="bs-panel" data-panel={panel.id}>
	{#if panel.id === 'status'}
		<StatusOverview />
	{:else if panel.id === 'farm-jobs'}
		<section class="panel"><FarmJobsPanel /></section>
	{:else if panel.id === 'sources'}
		<section class="panel"><h3>Lyric sources</h3><LyricSourceOrder /></section>
	{:else if panel.id === 'jobs'}
		<section class="panel"><h3>Lyric jobs</h3><LyricJobs /></section>
	{:else if panel.id === 'triage'}
		<ReviewPanel />
	{:else if panel.id === 'coverage'}
		<LyricsKpiPanel />
	{:else if panel.id === 'perf'}
		{@render perfTable()}
	{:else if panel.id === 'farm'}
		{@render farmTable()}
	{:else if panel.id === 'quality'}
		<QualityRatchet />
	{:else if panel.id === 'flags'}
		<EntitlementsInspector />
	{:else if panel.id === 'preflight'}
		<PreflightScreen mode="admin" navigate={goto} />
	{:else if panel.id === 'health' || panel.id === 'bus' || panel.id === 'ports'}
		{#await import('./DiagnosticsPanel.svelte') then { default: DiagnosticsPanel }}
			<DiagnosticsPanel />
		{/await}
	{:else if panel.id === 'api' || panel.id === 'sql'}
		{#await import('./PlaygroundPanel.svelte') then { default: PlaygroundPanel }}
			<PlaygroundPanel />
		{/await}
	{/if}
	<TipLayer />
</div>
{:else if panel.kind === 'unknown'}
<h2>Backstage</h2>
<div class="fatal" title={`Known panels: ${ADMIN_PANEL_IDS.join(', ')}`}>
	UNKNOWN PANEL {panel.id} - known panels: {ADMIN_PANEL_IDS.join(', ')}
</div>
{:else}
<h2>Admin</h2>

<!-- A div, not a nav: a nav is non-interactive and cannot carry role=tablist.
     Same shape as the browser's PaneTabs strip. -->
<div class="admin-chrome">
<div class="admin-tabs" role="tablist" aria-label="admin sections">
	<button
		type="button"
		id="admin-tab-kpi"
		class="admin-tab"
		class:on={tab === 'kpi'}
		role="tab"
		aria-selected={tab === 'kpi'}
		aria-controls="admin-panel-kpi"
		title="The demucs farm KPI ledger, served by GET /api/v1/bench/kpi. This tab is the page you are on."
		onclick={() => selectTab('kpi')}
	>
		KPI ledger
	</button>
	<button
		type="button"
		id="admin-tab-lyrics"
		class="admin-tab"
		class:on={tab === 'lyrics'}
		role="tab"
		aria-selected={tab === 'lyrics'}
		aria-controls="admin-panel-lyrics"
		title="Lyric source order, generation jobs, triage, and coverage KPIs."
		onclick={() => selectTab('lyrics')}
	>
		Lyrics generator
	</button>
	<button
		type="button"
		id="admin-tab-quality"
		class="admin-tab"
		class:on={tab === 'quality'}
		role="tab"
		aria-selected={tab === 'quality'}
		aria-controls="admin-panel-quality"
		title="The code-quality ratchet the merge gate enforces on every commit."
		onclick={() => selectTab('quality')}
	>
		Quality ratchet
	</button>
	<button
		type="button"
		id="admin-tab-diagnostics"
		class="admin-tab"
		class:on={tab === 'diagnostics'}
		role="tab"
		aria-selected={tab === 'diagnostics'}
		aria-controls="admin-panel-diagnostics"
		title="Daemon health, capability probe, event bus, and worktree ports."
		onclick={() => selectTab('diagnostics')}
	>
		Diagnostics
	</button>
	<button
		type="button"
		id="admin-tab-playground"
		class="admin-tab"
		class:on={tab === 'playground'}
		role="tab"
		aria-selected={tab === 'playground'}
		aria-controls="admin-panel-playground"
		title="Hit /api/v1/* and run read-only SQL against state.db."
		onclick={() => selectTab('playground')}
	>
		Playground
	</button>
	<button
		type="button"
		id="admin-tab-spotify"
		class="admin-tab"
		class:on={tab === 'spotify'}
		role="tab"
		aria-selected={tab === 'spotify'}
		aria-controls="admin-panel-spotify"
		title="Spotify connection probe and live listen history, served by GET /api/v1/spotify/status and GET /api/v1/spotify/recently-played."
		onclick={() => selectTab('spotify')}
	>
		Spotify
	</button>
	<button
		type="button"
		id="admin-tab-title-fix"
		class="admin-tab"
		class:on={tab === 'title-fix'}
		role="tab"
		aria-selected={tab === 'title-fix'}
		aria-controls="admin-panel-title-fix"
		title="Fix tracks whose artist, key or version is jammed into the title: scan, review, apply, undo."
		onclick={() => selectTab('title-fix')}
	>
		Title fixes
	</button>
</div>
<button
	type="button"
	class="admin-setup-action"
	onclick={() => void onSetupTab()}
	disabled={setupBusy || setupBlocked !== null}
	title={setupBlocked ?? RUN_SETUP_TITLE}
>
	{setupBusy ? 'Opening setup...' : 'Setup'}
</button>
{#if setupError !== null}
	<span class="admin-tab-err" title={setupError}>{setupError}</span>
{/if}
</div>

{#if tab === 'kpi'}
<div id="admin-panel-kpi" role="tabpanel" aria-labelledby="admin-tab-kpi">
{@render farmPanel()}
{@render perfPanel()}
<EntitlementsInspector />

<!-- PREFLIGHT-01 (#771): read-only, always-live-polling reference. Same
     GET /api/v1/preflight the boot gate reads; no navigation side effect. -->
<PreflightScreen mode="admin" navigate={goto} />
</div>
{:else if tab === 'lyrics'}
<div id="admin-panel-lyrics" role="tabpanel" aria-labelledby="admin-tab-lyrics">
{@render lyricsPanel()}
{@render triagePanel()}
<LyricsKpiPanel />
</div>
{:else if tab === 'quality'}
<div id="admin-panel-quality" role="tabpanel" aria-labelledby="admin-tab-quality">
<QualityRatchet />
</div>
{:else if tab === 'diagnostics'}
	{#await import('./DiagnosticsPanel.svelte') then { default: DiagnosticsPanel }}
		<DiagnosticsPanel />
	{/await}
{:else if tab === 'playground'}
	{#await import('./PlaygroundPanel.svelte') then { default: PlaygroundPanel }}
		<PlaygroundPanel />
	{/await}
{:else if tab === 'spotify'}
	{#await import('./SpotifyPanes.svelte') then { default: SpotifyPanes }}
		<SpotifyPanes />
	{/await}
{:else if tab === 'title-fix'}
	{#await import('./TitleFixPanel.svelte') then { default: TitleFixPanel }}
		<TitleFixPanel />
	{/await}
{/if}
<TipLayer />
{/if}

{#snippet farmPanel()}
<section class="panel">
	<h3>Demucs farm KPI ledger</h3>
	<p class="sub">
		One card per KPI, tracked across farm runs. The latest reading is the big number and the arrow
		is the move since the previous reading, green when it moved the good way for that KPI, red when
		it moved the wrong way. Hover anywhere on a card for what the number means and why it matters.
	</p>
	<p class="note">
		A dash or a hollow dashed mark in a sparkline means that KPI was not measured in that run, which
		is different from a real zero. <code>fixed_overhead_s</code> is genuinely 0 on Modal (no VM to
		boot); that is not the same thing as unmeasured. Durations render as
		<code>23m 24s</code> rather than raw seconds; sub-minute readings stay in seconds.
	</p>
	<p class="note">
		Cards with no badge are <strong>derived</strong>: recomputed from per-track cache telemetry by
		<code>scripts/bench/kpi_derive.py</code>, so nobody typed them. A <code>typed</code> badge means
		hand-entered with no telemetry behind it, and <code>config</code> means the value was copied
		from a config constant and is a ceiling, not a measurement. Hover for the exact origin.
	</p>

	{#if error}
		<div class="fatal">
			LOAD FAILED

			{error}

			The daemon serves this ledger from (private path). Check that the API is up on
			:8585 and that the file parses.
		</div>
	{:else if !ledger}
		<p class="sub">Loading ledger...</p>
	{:else}
		<div class="grid">
			{#each metrics as [metric, kpi] (metric)}
				<KpiTile {metric} {kpi} snapshots={ledger.snapshots} />
			{/each}
		</div>

		<RunNotes kpis={ledger.kpis} snapshots={ledger.snapshots} />

		<p class="footer">
			{ledger.snapshots.length} snapshot(s); latest <code>{latestRun?.label}</code> at
			<code>{latestRun?.ts}</code>. Append with
			<code>uv run scripts/bench/kpi_append.py --label &lt;run&gt; --set key=value --note "..."</code>.
		</p>
	{/if}
</section>

{/snippet}
{#snippet perfPanel()}
<section class="panel perf-panel">
	<h3>Performance KPIs</h3>
	{#if perfError}
		<div class="fatal">
			LOAD FAILED

			{perfError}

			The daemon serves these cards from (private path) and (private path) via
			GET /api/v1/bench/perf-kpi.
		</div>
	{:else if !perf}
		<p class="sub">Loading perf ledger...</p>
	{:else}
		<p class="sub">
			The openDJ latency and RAM program, tracked since <strong>{perf.sinceLabel}</strong>, the first
			perf measurement. Same cards as above: the big number is the latest reading, the arrow is the
			move since the previous one, and the sparkline places each reading by calendar date from the
			first measurement to today so gaps match the time between them. A card whose latest reading is
			old shows how many days old. Cards that have moved come first.
		</p>
		<p class="note">
			Withheld and superseded readings are gaps, not numbers. Whether each UX scenario meets its
			budget is a separate question: <code>just perf-kpis</code> scores them, and most still read
			UNMEASURED.
		</p>
		{#if perf.undeclared.length > 0}
			<p class="warn" title="Declare these in (private path) with a label, direction and title.">
				UNDECLARED: {perf.undeclared.join(', ')} - recorded in the ledger but not shown, because no card
				says which way is better.
			</p>
		{/if}
		<div class="grid">
			{#each perfMetrics as [metric, kpi] (metric)}
				<KpiTile {metric} {kpi} snapshots={perf.snapshots} timeAxis />
			{/each}
		</div>

		<RunNotes kpis={perf.kpis} snapshots={perf.snapshots} />

		<p class="footer">
			{perfMetrics.length} KPI(s) over {perf.snapshots.length} measurement round(s). Append with
			<code>scripts/perf/kpi_ledger_append.py</code>.
		</p>
	{/if}
</section>

{/snippet}
{#snippet perfTable()}
{#if perfError}
	<div class="fatal">LOAD FAILED {perfError} - GET /api/v1/bench/perf-kpi ((private path) + kpi-cards.json)</div>
{:else if !perf}
	<p class="sub">Loading perf ledger...</p>
{:else}
	<KpiTable
		name="Performance KPIs"
		kpis={perf.kpis}
		snapshots={perf.snapshots}
		timeAxis
		source={`Latency and RAM program KPIs since ${perf.sinceLabel}, from (private path) via GET /api/v1/bench/perf-kpi.${perf.undeclared.length > 0 ? ` Undeclared, not shown: ${perf.undeclared.join(', ')}.` : ''}`}
	/>
{/if}
{/snippet}
{#snippet farmTable()}
{#if error}
	<div class="fatal">LOAD FAILED {error} - GET /api/v1/bench/kpi ((private path))</div>
{:else if !ledger}
	<p class="sub">Loading ledger...</p>
{:else}
	<KpiTable
		name="Stem farm"
		kpis={ledger.kpis}
		snapshots={ledger.snapshots}
		banner
		source="Demucs farm KPIs per run, from (private path) via GET /api/v1/bench/kpi. A typed badge is hand-entered; config is a ceiling, not a measurement."
	/>
{/if}
{/snippet}
{#snippet lyricsPanel()}
<section class="panel" id="lyrics-generator">
	<h3>Lyrics generator</h3>
	<p class="sub">
		The generator pipeline runs offline: stems (vocal separation) -&gt; alignment (word timings)
		-&gt; witness (independent ASR cross-check) -&gt; ingest into state.db. Order the lyric sources
		below to steer where the text comes from, and queue per-track generation here or from the
		library right-click menu.
	</p>
	<LyricSourceOrder />
	<hr />
	<LyricJobs />
</section>

{/snippet}
{#snippet triagePanel()}
<section class="panel">
	<h3>Lyric triage</h3>
	<p class="sub">
		Review and repair: the tracks whose lyrics deserve human eyes first, ranked most suspect
		first. Click a row to open the track page with the full lyric panel; use the override buttons
		to record your verdict - an override beats the computed one everywhere.
	</p>
	<LyricTriage />
</section>

{/snippet}

<style>
	/* No success/positive token exists in app.css yet; scoped here rather than
	 * editing that shared file mid-fan-out. */
	:global(:root) {
		--kpi-ok: #4ecb8c;
	}
	:global(html:is([data-theme='light'], [data-skin='light'])) {
		/* #2e9e63 was 3.0:1 on the light panels under small text; the light
		 * shell's --success clears AA. */
		--kpi-ok: var(--success);
	}

	.admin-chrome {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 8px;
		margin: 0.5rem 0 1rem;
		border-bottom: 1px solid var(--border, #1c222c);
		padding-bottom: 0;
	}
	.admin-tabs {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 4px;
		flex: 1 1 auto;
	}
	.admin-setup-action {
		padding: 6px 14px;
		border: 1px solid var(--border, #1c222c);
		border-radius: 6px;
		background: var(--chip-bg);
		color: var(--fg);
		font: inherit;
		font-size: 0.9rem;
		cursor: pointer;
		margin-left: auto;
	}
	.admin-setup-action:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.admin-setup-action:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.admin-tab {
		padding: 6px 14px;
		border: 1px solid transparent;
		border-bottom: none;
		border-radius: 6px 6px 0 0;
		background: transparent;
		color: var(--muted);
		font: inherit;
		font-size: 0.9rem;
		cursor: pointer;
	}
	.admin-tab:hover:not(:disabled) {
		color: var(--fg);
	}
	.admin-tab.on {
		background: var(--chip-bg);
		border-color: var(--border, #1c222c);
		color: var(--accent);
	}
	.admin-tab:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.admin-tab-err {
		font-size: 0.78rem;
		color: var(--danger);
	}
	.panel {
		max-width: 1180px;
	}
	h3 {
		font-size: 1rem;
		color: var(--accent);
		margin: 0 0 0.25rem 0;
	}
	.sub {
		color: var(--muted);
		font-size: 0.85rem;
		margin: 0 0 0.5rem 0;
		max-width: 90ch;
	}
	.note {
		color: var(--muted);
		font-size: 0.78rem;
		margin: 0 0 1.1rem 0;
		max-width: 90ch;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
		gap: 0.75rem;
	}
	.perf-panel {
		margin-top: 2rem;
	}
	.warn {
		color: var(--danger);
		font-size: 0.8rem;
		font-weight: 600;
		margin: 0 0 1rem 0;
	}
	.footer {
		color: var(--muted);
		font-size: 0.75rem;
		margin-top: 1.25rem;
	}
	.fatal {
		background: var(--danger);
		color: #fff;
		padding: 1rem 1.25rem;
		border-radius: 8px;
		font-weight: 600;
		white-space: pre-wrap;
		line-height: 1.5;
	}
	code {
		background: var(--chip-bg);
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
	}
</style>
