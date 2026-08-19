<script lang="ts">
	import { onMount } from 'svelte';

	import { goto } from '$app/navigation';
	import { jobsStore, errorTail } from '$lib/rb/jobs-store.svelte';
	import {
		STAGE_LABELS,
		accessCaveat,
		blockerSentence,
		formatBytes,
		setupRefusal
	} from '$lib/setup/setup-api';
	import {
		STEP_TITLES,
		WIZARD_STEPS,
		advanceRefusal,
		fatalBlockers,
		importPct,
		setupWizard,
		stepIndex
	} from '$lib/setup/wizard.svelte';

	const refusal = $derived(setupRefusal());
	const step = $derived(setupWizard.step);
	const detection = $derived(setupWizard.detection);
	const status = $derived(setupWizard.status);
	const stems = $derived(setupWizard.stems);

	/** The job row, straight out of the jobs store. Never a local copy: the
	 * store is fed by the engine's events bus and duplicating the row here
	 * would give the page two truths about one import. */
	const job = $derived(
		setupWizard.jobId === null
			? null
			: (jobsStore.jobs.find((row) => row.id === setupWizard.jobId) ?? null)
	);

	const lastImport = $derived(status?.last_import ?? null);
	const permissions = $derived(status?.permissions ?? null);
	const deniedRoots = $derived(permissions?.denied ?? []);
	const caveat = $derived(accessCaveat(permissions));
	const blockers = $derived(detection?.blockers ?? []);
	const fatal = $derived(fatalBlockers(detection));
	const nextRefusal = $derived(advanceRefusal(step, { detection, job }));
	const pct = $derived(importPct(job));

	let refreshDecrypt = $state(false);
	let stemsChoice = $state<'now' | 'later'>('later');

	onMount(() => {
		void setupWizard.load();
	});

	/** The jobs store is the progress feed. Attached only while the progress
	 * step is on screen and only when the daemon offers the jobs API, so a
	 * legacy boot never opens a socket it cannot use. */
	$effect(() => {
		if (setupWizard.step !== 'progress') return;
		if (refusal !== null) return;
		return jobsStore.attach();
	});

	$effect(() => {
		if (setupWizard.step !== 'stems') return;
		if (setupWizard.stems !== null) return;
		void setupWizard.loadStems();
	});

	async function skipWizard(): Promise<void> {
		await setupWizard.skip();
		if (setupWizard.error === null) await goto('/');
	}

	async function finish(): Promise<void> {
		await setupWizard.skip();
		if (setupWizard.error === null) await goto('/');
	}

	function probeLine(label: string, path: string, exists: boolean, size: number | null): string {
		const bytes = formatBytes(size);
		return exists
			? `${label}: ${path}${bytes === null ? '' : ` (${bytes})`}`
			: `${label}: not present at ${path}`;
	}
</script>

<section class="wizard" aria-label="First-run setup">
	<h2>First-run setup</h2>

	<ol class="steps">
		{#each WIZARD_STEPS as name (name)}
			<li
				class="step"
				class:current={name === step}
				class:past={stepIndex(name) < stepIndex(step)}
				aria-current={name === step ? 'step' : undefined}
				title={`Step ${stepIndex(name) + 1} of ${WIZARD_STEPS.length}`}
			>
				{STEP_TITLES[name]}
			</li>
		{/each}
	</ol>

	{#if refusal !== null}
		<p class="refusal" role="status">{refusal}</p>
	{/if}

	{#if setupWizard.error !== null}
		<p class="error" role="alert">{setupWizard.error}</p>
	{/if}

	<!-- ---------------------------------------------------------- welcome -->
	{#if step === 'welcome'}
		<div class="panel">
			<p>
				This engine has a library database of its own. Setting it up means
				reading your existing rekordbox collection into it: tracks, playlists,
				BPM, key and rating. Nothing in rekordbox is written to or changed --
				the import only ever reads a copy.
			</p>
			{#if status !== null}
				<p class="counts">
					Library right now:
					<strong title="Tracks currently in the engine's state database">
						{status.tracks} tracks
					</strong>,
					<strong title="Playlists currently in the engine's state database">
						{status.playlists} playlists
					</strong>.
					Data directory <code>{status.data_dir}</code>.
				</p>
				{#if lastImport !== null}
					<p class="muted">
						An import already ran here, finishing
						<span title="When the last import finished (UTC)">
							{lastImport.finished_at}
						</span>.
					</p>
				{/if}
			{/if}
			<div class="actions">
				<button type="button" onclick={() => setupWizard.next()} disabled={setupWizard.busy}>
					Get started
				</button>
				<button type="button" class="secondary" onclick={skipWizard} disabled={setupWizard.busy}>
					Skip for now
				</button>
			</div>
		</div>
	{/if}

	<!-- ----------------------------------------------------------- detect -->
	{#if step === 'detect'}
		<div class="panel">
			<h3>What is on this machine</h3>
			{#if detection === null}
				<p class="muted">Looking...</p>
			{:else}
				<ul class="probes">
					<li>
						{probeLine(
							'rekordbox database',
							detection.live_db.path,
							detection.live_db.exists,
							detection.live_db.size_bytes ?? null
						)}
					</li>
					<li>
						{probeLine(
							'Analysis folder',
							detection.share_dir.path,
							detection.share_dir.exists,
							null
						)}
					</li>
					<li>
						{probeLine(
							'Encrypted working copy',
							detection.working_copy.path,
							detection.working_copy.exists,
							detection.working_copy.size_bytes ?? null
						)}
					</li>
					<li>
						{probeLine(
							'Decrypted working copy',
							detection.plain_copy.path,
							detection.plain_copy.exists,
							detection.plain_copy.size_bytes ?? null
						)}
					</li>
					<li>Database key: {detection.key_detail}</li>
				</ul>

				{#if detection.import_source !== null}
					<p>
						The import will read <code>{detection.import_source}</code>
						{#if detection.import_source_encrypted}
							, which is encrypted and will be decrypted first.
						{:else}
							, which is already decrypted.
						{/if}
					</p>
				{/if}

				{#if detection.rekordbox_running}
					<p class="muted">
						rekordbox is running. That is fine -- the import reads a copy -- but
						anything you change in rekordbox from now on will not be in it.
					</p>
				{/if}

				{#each blockers as code (code)}
					<p class="blocker" class:fatal={fatal.includes(code)} role="alert">
						{blockerSentence(code, detection)}
					</p>
				{/each}
			{/if}

			<div class="actions">
				<button type="button" class="secondary" onclick={() => setupWizard.back()}>Back</button>
				<button
					type="button"
					class="secondary"
					onclick={() => setupWizard.redetect()}
					disabled={setupWizard.busy}
				>
					Look again
				</button>
				<button
					type="button"
					onclick={() => setupWizard.next()}
					disabled={nextRefusal !== null || setupWizard.busy}
					title={nextRefusal ?? 'Continue to the import'}
				>
					Continue
				</button>
			</div>
		</div>
	{/if}

	<!-- ---------------------------------------------------------- confirm -->
	{#if step === 'confirm'}
		<div class="panel">
			<h3>Confirm the import</h3>
			{#if detection !== null && detection.import_source !== null}
				<p>
					Reading <code>{detection.import_source}</code> into
					<code>{status?.data_dir ?? 'the data directory'}</code>.
				</p>
			{/if}
			<p>These are the stages it will report:</p>
			<ol class="stages">
				{#each status?.stages ?? [] as stage (stage)}
					<li><strong>{stage}</strong> {STAGE_LABELS[stage] ?? ''}</li>
				{/each}
			</ol>
			{#if detection?.plain_copy.exists}
				<label class="checkbox">
					<input type="checkbox" bind:checked={refreshDecrypt} />
					Decrypt again instead of reusing the existing
					<code>master.plain.db</code>
				</label>
			{/if}
			<div class="actions">
				<button type="button" class="secondary" onclick={() => setupWizard.back()}>Back</button>
				<button
					type="button"
					onclick={() => setupWizard.beginImport({ refreshDecrypt })}
					disabled={setupWizard.busy || refusal !== null}
					title={refusal ?? 'Start the import as a background job'}
				>
					Start the import
				</button>
			</div>
		</div>
	{/if}

	<!-- --------------------------------------------------------- progress -->
	{#if step === 'progress'}
		<div class="panel">
			<h3>Importing</h3>
			{#if job === null}
				<p class="muted">
					Waiting for the engine to report on job
					<code>{setupWizard.jobId ?? '(none started)'}</code>. Nothing has come
					back yet.
				</p>
			{:else}
				<div
					class="bar"
					role="progressbar"
					aria-valuenow={pct}
					aria-valuemin="0"
					aria-valuemax="100"
					aria-label="Import progress"
					title={`Import progress: ${pct}% of the way through the five stages`}
				>
					<div class="fill" style={`width: ${pct}%`}></div>
					<span class="pct" title={`Import progress: ${pct}% of the five stages`}>
						{pct}%
					</span>
				</div>
				<p class="status-line">
					<span title="The job's current status as the engine last wrote it">
						{job.status}
					</span>
					{#if job.message !== null && job.message !== undefined}
						-- {job.message}
					{/if}
				</p>
				{#if job.error !== null && job.error !== undefined}
					<pre class="error-tail">{errorTail(job.error)}</pre>
				{/if}
			{/if}
			<div class="actions">
				<button
					type="button"
					onclick={() => setupWizard.next()}
					disabled={nextRefusal !== null}
					title={nextRefusal ?? 'Continue'}
				>
					Continue
				</button>
			</div>
		</div>
	{/if}

	<!-- ------------------------------------------------------------ stems -->
	{#if step === 'stems'}
		<div class="panel">
			<h3>Stems analysis</h3>
			<p>
				Stem separation splits a track into vocals, drums, bass and the rest.
				It is what makes acapella and instrumental playback possible.
			</p>

			{#if stems === null}
				<p class="muted">Asking the engine what it can run...</p>
			{:else}
				<fieldset class="choice" disabled={!stems.available}>
					<legend>Analyse the library after the import?</legend>
					<label>
						<input type="radio" bind:group={stemsChoice} value="now" />
						Yes, start separating the whole library now
					</label>
					<label>
						<input type="radio" bind:group={stemsChoice} value="later" />
						No, I will do individual tracks later
					</label>
				</fieldset>

				{#if !stems.available}
					<p class="blocker" role="status">{stems.reason}</p>
					<p class="muted">
						Individual tracks can already be separated from the track menu,
						which posts to <code>{stems.per_track_endpoint}</code>. What does
						not exist yet is anything that runs it across a whole library, so
						this choice cannot be acted on.
					</p>
				{/if}

				<h4>Separation tiers this engine knows about</h4>
				<table class="library tiers">
					<thead>
						<tr><th>Tier</th><th>Runs</th><th>Available</th></tr>
					</thead>
					<tbody>
						{#each stems.tiers as tier (tier.key)}
							<tr>
								<td><code>{tier.key}</code> {tier.name}</td>
								<td>{tier.where}</td>
								<td title={tier.unavailable_because || 'This tier can run'}>
									{tier.availability}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}

			<div class="actions">
				<button type="button" class="secondary" onclick={() => setupWizard.back()}>Back</button>
				<button type="button" onclick={() => setupWizard.next()}>Continue</button>
			</div>
		</div>
	{/if}

	<!-- ------------------------------------------------------------- done -->
	{#if step === 'done'}
		<div class="panel">
			<h3>Done</h3>
			{#if lastImport !== null}
				<p class="counts">
					Imported
					<strong title="Tracks written into the engine's state database">
						{lastImport.tracks} tracks
					</strong>
					and
					<strong title="Playlists written into the engine's state database">
						{lastImport.playlists} playlists
					</strong>.
				</p>
				<p class="muted">
					<span
						title="Rekordbox analysis files (waveforms, beatgrids) found on disk, out of the imported tracks that name one"
					>
						{lastImport.analyses_linked} of
						{lastImport.analyses_expected}
					</span>
					analyses were found under
					<code>{lastImport.share_root}</code>.
					{#if lastImport.analyses_linked === 0 && lastImport.analyses_expected > 0}
						None resolved, so waveforms will not draw until that folder is
						reachable.
					{/if}
				</p>
			{:else}
				<p class="muted">
					No import was recorded for this data directory. The library is
					whatever was already in it.
				</p>
			{/if}
			<div class="actions">
				<button type="button" onclick={finish} disabled={setupWizard.busy}>
					Open the library
				</button>
			</div>
		</div>
	{/if}

	<p class="muted footnote">
		Every step here is an HTTP endpoint under <code>/api/v1/setup</code>, so
		this whole flow can be driven without a browser.
	</p>
</section>

<style>
	.wizard {
		max-width: 60rem;
	}
	.steps {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		list-style: none;
		padding: 0;
		margin: 0 0 1rem;
	}
	.step {
		padding: 0.2rem 0.6rem;
		border: 1px solid var(--border);
		border-radius: 999px;
		color: var(--muted);
		font-size: 0.85rem;
	}
	.step.past {
		color: var(--fg);
		border-color: var(--accent-dim);
	}
	.step.current {
		color: var(--bg);
		background: var(--accent);
		border-color: var(--accent);
	}
	.panel {
		border: 1px solid var(--border);
		background: var(--surface);
		border-radius: 6px;
		padding: 1rem 1.25rem;
	}
	.actions {
		display: flex;
		gap: 0.5rem;
		margin-top: 1.25rem;
	}
	.secondary {
		background: transparent;
		color: var(--fg);
		border: 1px solid var(--border);
	}
	.probes,
	.stages {
		line-height: 1.7;
	}
	.probes {
		list-style: none;
		padding: 0;
	}
	.muted {
		color: var(--muted);
	}
	.refusal,
	.blocker {
		color: var(--muted);
		border-left: 3px solid var(--accent-dim);
		padding-left: 0.6rem;
	}
	.blocker.fatal,
	.error {
		color: var(--danger);
		border-left: 3px solid var(--danger);
		padding-left: 0.6rem;
	}
	.bar {
		position: relative;
		height: 1.4rem;
		border: 1px solid var(--border);
		border-radius: 3px;
		overflow: hidden;
		background: var(--bg);
	}
	.fill {
		height: 100%;
		background: var(--accent-dim);
	}
	.pct {
		position: absolute;
		inset: 0;
		display: grid;
		place-items: center;
		font-size: 0.8rem;
	}
	.status-line {
		margin-top: 0.5rem;
	}
	.error-tail {
		white-space: pre-wrap;
		color: var(--danger);
		font-size: 0.8rem;
	}
	.choice {
		border: 1px solid var(--border);
		display: grid;
		gap: 0.35rem;
		padding: 0.75rem 1rem;
	}
	.choice[disabled] {
		opacity: 0.55;
	}
	.tiers {
		margin-top: 0.5rem;
	}
	.checkbox {
		display: block;
		margin-top: 0.75rem;
	}
	.footnote {
		font-size: 0.8rem;
		margin-top: 1rem;
	}
</style>
