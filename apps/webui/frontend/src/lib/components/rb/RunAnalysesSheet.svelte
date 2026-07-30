<script lang="ts">
	// Provisional "Run analyses…" chooser (CreatePairingSheet-style sheet).
	// Vocals is live; other kinds are stubs until their workers land.
	import { findVocals, RbApiError, vocalsOf } from '$lib/rb/api-rb';
	import { jobProgress } from '$lib/rb/job-progress.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import { refreshAnlz } from './wave/anlz-cache.svelte';

	let {
		open = $bindable(false),
		stableIds = []
	}: {
		open?: boolean;
		stableIds?: string[];
	} = $props();

	type KindId = 'vocals' | 'beatgrid' | 'waveform' | 'stems' | 'lyrics';

	const KINDS: {
		id: KindId;
		label: string;
		enabled: boolean;
		hint: string;
	}[] = [
		{
			id: 'vocals',
			label: 'Vocals',
			enabled: true,
			hint: 'Local demucs regions into vocal-cache; merged on next /anlz.'
		},
		{
			id: 'beatgrid',
			label: 'Beatgrid',
			enabled: false,
			hint: 'Not wired yet - rekordbox PQTZ remains the source.'
		},
		{
			id: 'waveform',
			label: 'Waveform',
			enabled: false,
			hint: 'Not wired yet - ANLZ PWAV/PWV4 stay file-backed.'
		},
		{
			id: 'stems',
			label: 'Stems',
			enabled: false,
			hint: 'Not wired yet - stem artifacts are a separate path.'
		},
		{
			id: 'lyrics',
			label: 'Lyrics',
			enabled: false,
			hint: 'Use Get lyrics in the context menu for now.'
		}
	];

	let selected = $state<Record<KindId, boolean>>({
		vocals: true,
		beatgrid: false,
		waveform: false,
		stems: false,
		lyrics: false
	});
	let busy = $state(false);

	$effect(() => {
		if (!open) return;
		selected = {
			vocals: true,
			beatgrid: false,
			waveform: false,
			stems: false,
			lyrics: false
		};
		busy = false;
	});

	const targets = $derived(stableIds.filter((id) => id !== ''));
	const canRun = $derived(targets.length > 0 && selected.vocals && !busy);

	async function _markVocalsReady(stableId: string): Promise<boolean> {
		const entry = await refreshAnlz(stableId);
		if (entry.status !== 'ready') return false;
		const v = vocalsOf(entry.data);
		const done = v.status !== 'not_analyzed';
		if (done) {
			jobProgress.setBadge(stableId, 'vocals', true);
			jobProgress.clear(stableId, 'vocals');
		}
		return done;
	}

	async function _runVocalsOne(stableId: string): Promise<void> {
		jobProgress.upsert({
			stable_id: stableId,
			kind: 'vocals',
			phase: 'queued',
			progress: 0.02,
			label: 'vocals',
			real: false
		});
		try {
			const res = await findVocals(stableId);
			pushToast(`Vocals ${stableId.slice(0, 8)}...: ${res.message}`, 'info');
			if (res.status === 'cached') {
				jobProgress.upsert({
					stable_id: stableId,
					kind: 'vocals',
					phase: 'running',
					progress: 0.7,
					label: 'vocals',
					real: true
				});
				await _markVocalsReady(stableId);
				return;
			}
			jobProgress.upsert({
				stable_id: stableId,
				kind: 'vocals',
				phase: 'running',
				progress: 0.08,
				label: 'vocals',
				real: false
			});
			void _pollUntilVocals(stableId);
		} catch (exc) {
			const msg = exc instanceof RbApiError ? exc.message : String(exc);
			jobProgress.upsert({
				stable_id: stableId,
				kind: 'vocals',
				phase: 'error',
				progress: 0,
				error: msg
			});
			pushToast(`Vocals failed: ${msg}`, 'error');
		}
	}

	async function _pollUntilVocals(stableId: string): Promise<void> {
		const steps = [8_000, 20_000, 45_000, 90_000];
		for (let i = 0; i < steps.length; i++) {
			await new Promise((r) => setTimeout(r, steps[i]));
			jobProgress.upsert({
				stable_id: stableId,
				kind: 'vocals',
				phase: 'running',
				progress: Math.min(0.9, 0.15 + (i + 1) / steps.length),
				label: 'vocals',
				real: true
			});
			if (await _markVocalsReady(stableId)) return;
		}
	}

	async function _run(): Promise<void> {
		if (!canRun) return;
		busy = true;
		try {
			if (selected.vocals) {
				for (const id of targets) {
					await _runVocalsOne(id);
				}
			}
			open = false;
		} finally {
			busy = false;
		}
	}
</script>

{#if open}
	<div class="sheet" role="dialog" aria-label="Run analyses">
		<header>
			<strong>Run analyses…</strong>
			<button type="button" class="x" onclick={() => (open = false)}>×</button>
		</header>
		<p class="hint">
			{targets.length === 0
				? 'No tracks selected.'
				: targets.length === 1
					? '1 track'
					: `${targets.length} tracks`}
			- choose analyses below. Farm/import cache writes do not push into the UI;
			this path refetches /anlz so Preview bars update immediately.
		</p>
		<p class="help">
			Trade-offs (placeholder): Vocals is CPU-heavy demucs; Beatgrid/Waveform/Stems
			stubs stay disabled until their workers exist. Prefer running on a short
			selection rather than whole playlists until batch UX lands.
		</p>
		<ul>
			{#each KINDS as k (k.id)}
				<li>
					<label class:disabled={!k.enabled} title={k.hint}>
						<input
							type="checkbox"
							disabled={!k.enabled || busy}
							checked={selected[k.id]}
							onchange={() => {
								if (!k.enabled) return;
								selected = { ...selected, [k.id]: !selected[k.id] };
							}}
						/>
						<span class="label">{k.label}</span>
						{#if !k.enabled}<span class="tag">later</span>{/if}
					</label>
					<span class="kind-hint">{k.hint}</span>
				</li>
			{/each}
		</ul>
		<footer>
			<button type="button" class="ghost" disabled={busy} onclick={() => (open = false)}>
				Cancel
			</button>
			<button type="button" class="primary" disabled={!canRun} onclick={() => void _run()}>
				{busy ? 'Running…' : 'Run'}
			</button>
		</footer>
	</div>
{/if}

<style>
	.sheet {
		position: fixed;
		top: 48px;
		right: 12px;
		z-index: 10050;
		width: min(380px, calc(100vw - 24px));
		background: var(--rb-panel, #14171d);
		border: 1px solid var(--rb-border, #2a3038);
		border-radius: 4px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55);
		color: var(--rb-text, #c8cdd2);
		font-family: var(--rb-font, ui-sans-serif, system-ui, sans-serif);
		font-size: 12px;
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 10px 12px;
		border-bottom: 1px solid var(--rb-border, #2a3038);
	}
	.x {
		border: none;
		background: transparent;
		color: var(--rb-text-dim);
		font-size: 18px;
		cursor: pointer;
	}
	.hint,
	.help {
		margin: 8px 12px;
		color: var(--rb-text-dim);
		font-size: 11px;
		line-height: 1.35;
	}
	.help {
		margin-top: 0;
		opacity: 0.9;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0 8px 8px;
		max-height: 280px;
		overflow: auto;
	}
	li {
		padding: 6px 4px;
		border-bottom: 1px solid rgba(42, 48, 56, 0.6);
	}
	li:last-child {
		border-bottom: none;
	}
	label {
		display: flex;
		align-items: center;
		gap: 6px;
		cursor: pointer;
	}
	label.disabled {
		cursor: default;
		opacity: 0.55;
	}
	.label {
		font-weight: 600;
	}
	.tag {
		font-size: 9px;
		padding: 0 4px;
		border-radius: 2px;
		background: #3a4048;
		color: var(--rb-text-dim);
	}
	.kind-hint {
		display: block;
		margin: 2px 0 0 22px;
		font-size: 10px;
		color: var(--rb-text-dim);
		line-height: 1.3;
	}
	footer {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		padding: 10px 12px;
		border-top: 1px solid var(--rb-border, #2a3038);
	}
	footer button {
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		padding: 4px 10px;
		font-size: 11px;
		cursor: pointer;
	}
	.ghost {
		background: transparent;
		color: var(--rb-text);
	}
	.primary {
		background: var(--rb-accent, #3d7dd9);
		border-color: var(--rb-accent, #3d7dd9);
		color: #fff;
	}
	footer button:disabled {
		opacity: 0.45;
		cursor: default;
	}
</style>
