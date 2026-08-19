<script lang="ts">
	import { onMount } from 'svelte';

	import { goto } from '$app/navigation';
	import StemsPrompt from '$lib/components/rb/StemsPrompt.svelte';
	import { jobsStore, errorTail } from '$lib/rb/jobs-store.svelte';
	// StemsPrompt paints from the --rb-* palette, which theme.css scopes under
	// .perf-root on purpose so it cannot leak into the app's own accent. The
	// mount below is wrapped in that class; without this import every colour
	// var it reads would be undefined.
	import '$lib/rb/theme.css';
	import {
		FOLDER_STAGE_LABELS,
		STAGE_LABELS,
		accessCaveat,
		blockerSentence,
		folderVerdict,
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

	/** The job row, straight out of the jobs store. Never a local copy: the
	 * store is fed by the engine's events bus and duplicating the row here
	 * would give the page two truths about one import. */
	const job = $derived(
		setupWizard.jobId === null
			? null
			: (jobsStore.jobs.find((row) => row.id === setupWizard.jobId) ?? null)
	);

	const lastImport = $derived(status?.last_import ?? null);
	/** Folders macOS refused during the LAST import, whichever kind it was.
	 * Both outcome models carry the list under their own name; the caveat it
	 * feeds is the same sentence either way. */
	const importDenied = $derived(
		lastImport === null
			? []
			: lastImport.kind === 'folder'
				? (lastImport.unreadable_roots ?? [])
				: (lastImport.unreadable_music_roots ?? [])
	);
	const permissions = $derived(status?.permissions ?? null);
	const deniedRoots = $derived(permissions?.denied ?? []);
	const caveat = $derived(accessCaveat(permissions));
	const blockers = $derived(detection?.blockers ?? []);
	const fatal = $derived(fatalBlockers(detection));
	const source = $derived(setupWizard.source);
	const folderScan = $derived(setupWizard.folderScan);
	const nextRefusal = $derived(
		advanceRefusal(step, { source, detection, folderScan, job })
	);
	const pct = $derived(importPct(job));
	const stageLabels = $derived(source === 'folder' ? FOLDER_STAGE_LABELS : STAGE_LABELS);
	const stageNames = $derived(
		(source === 'folder' ? status?.folder_stages : status?.stages) ?? []
	);

	let refreshDecrypt = $state(false);
	let folderInput = $state('');
	/** The separation job StemsPrompt started, if the tester said yes. Held
	 * only so the done screen can name it; the TopBar bar owns its progress. */
	let stemsJobId = $state<string | null>(null);

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
			{#if deniedRoots.length > 0}
				<p class="blocker fatal" role="alert">
					{caveat}
				</p>
				<p class="muted">{permissions?.how_to_grant}</p>
			{/if}

			<fieldset class="choice">
				<legend>Where is your music coming from?</legend>
				<label>
					<input
						type="radio"
						name="import-source"
						checked={source === 'rekordbox'}
						onchange={() => setupWizard.useSource('rekordbox')}
					/>
					A rekordbox collection on this machine
				</label>
				<label>
					<input
						type="radio"
						name="import-source"
						checked={source === 'folder'}
						onchange={() => setupWizard.useSource('folder')}
					/>
					A folder of audio files (no rekordbox needed)
				</label>
			</fieldset>
		</div>
	{/if}

	{#if step === 'detect' && source === 'folder'}
		<div class="panel">
			<h3>Point at a folder</h3>
			<p class="muted">
				This reads tags only. No BPM, no key and no beatgrid are written,
				and none are guessed -- the library you get is unanalysed, and the
				last screen will say so.
			</p>
			<form class="folder-form" onsubmit={(event) => event.preventDefault()}>
				<input
					type="text"
					placeholder="/Users/you/Music"
					bind:value={folderInput}
					aria-label="Folder to import"
				/>
				<button
					type="submit"
					onclick={() => setupWizard.checkFolder(folderInput)}
					disabled={setupWizard.busy || refusal !== null}
					title={refusal ?? 'Look inside this folder without importing it'}
				>
					Check this folder
				</button>
			</form>

			{#if folderScan !== null}
				<p class="blocker" class:fatal={folderScan.denied} role="status">
					{folderVerdict(folderScan)}
				</p>
				{#if folderScan.readable}
					<p class="counts">
						<strong
							title="Readable audio files found under this folder. iCloud placeholders are counted separately and never opened."
						>
							{folderScan.audio_files} audio files
						</strong>
						{#if folderScan.icloud_placeholders > 0}
							,
							<strong
								title="Files that exist but whose bytes live in iCloud. They are skipped, never downloaded."
							>
								{folderScan.icloud_placeholders} iCloud placeholders skipped
							</strong>
						{/if}
					</p>
					<ul class="probes">
						{#each folderScan.sample as example (example)}
							<li><code>{example}</code></li>
						{/each}
					</ul>
				{/if}
			{/if}

			<div class="actions">
				<button type="button" class="secondary" onclick={() => setupWizard.back()}>Back</button>
				<button
					type="button"
					onclick={() => setupWizard.beginFolderImport()}
					disabled={nextRefusal !== null || setupWizard.busy}
					title={nextRefusal ?? 'Import this folder'}
				>
					Import this folder
				</button>
			</div>
		</div>
	{/if}

	{#if step === 'detect' && source === 'rekordbox'}
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
				{#each stageNames as stage (stage)}
					<li><strong>{stage}</strong> {stageLabels[stage] ?? ''}</li>
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

			<!--
				MOUNTED, not rebuilt. StemsPrompt is af--stems-modal's component and
				the props below are the entire contract between the two lanes. It
				refuses to ask the question until GET /api/v1/stems/plan has told it
				how many tracks, how long and how much, so this wizard deliberately
				pre-fetches nothing and passes no numbers in -- a second source for
				those figures is a second thing that can disagree with the run.

				Once it enqueues, tracks light up one at a time through
				library.changed and the TopBar bar owns the progress. Nothing here
				polls.
			-->
			<div class="perf-root stems-mount">
				<StemsPrompt
					onenqueued={(jobId) => {
						stemsJobId = jobId;
						setupWizard.next();
					}}
					onskip={() => setupWizard.next()}
				/>
			</div>

			{#if stemsJobId !== null}
				<p class="muted" title={`Engine job ${stemsJobId}`}>
					Separation is running in the background as job
					<code>{stemsJobId}</code>. You can finish setup now.
				</p>
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
			{#if lastImport !== null && lastImport.kind === 'rekordbox'}
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
				{#if importDenied.length > 0}
					<p class="blocker fatal" role="alert">
						macOS blocked
						<span title="Music folders that could not be listed during the import">
							{importDenied.length}
						</span>
						folder(s) during this import ({importDenied.join(', ')}), so the
						counts above cover only what could be read.
					</p>
				{/if}
			{:else if lastImport !== null && lastImport.kind === 'folder'}
				<p class="counts">
					Imported
					<strong title="Tracks written into the engine's state database">
						{lastImport.tracks_written} tracks
					</strong>
					from
					<strong title="Readable audio files found under the folders you chose">
						{lastImport.files_seen} readable audio files
					</strong>.
				</p>
				<p class="blocker" role="status">
					<span
						title="Imported tracks with no BPM, key or beatgrid. A folder import reads tags only."
					>
						{lastImport.tracks_without_analysis}
					</span>
					of them have no analysis at all. {lastImport.analysis_detail}.
				</p>
				{#if lastImport.files_dataless > 0}
					<p class="muted">
						<span title="Files present but stored in iCloud with no local copy. They were skipped, never downloaded.">
							{lastImport.files_dataless}
						</span>
						iCloud placeholders were skipped rather than downloaded.
					</p>
				{/if}
				{#if importDenied.length > 0}
					<p class="blocker fatal" role="alert">
						macOS blocked {importDenied.join(', ')}, so the counts above cover
						only what could be read.
					</p>
				{/if}
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
	.stems-mount {
		margin-top: 0.75rem;
	}
	.checkbox {
		display: block;
		margin-top: 0.75rem;
	}
	.folder-form {
		display: flex;
		gap: 0.5rem;
		margin-top: 0.75rem;
	}
	.folder-form input {
		flex: 1 1 auto;
		min-width: 18rem;
	}
	.footnote {
		font-size: 0.8rem;
		margin-top: 1rem;
	}
</style>
