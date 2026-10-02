<script lang="ts">
	/**
	 * First-run setup, as an OVERLAY over the live app.
	 *
	 * WHAT CHANGED AND WHY. This was /setup, a route. A route is the wrong
	 * shape for a first run: it navigates someone away from the app before
	 * they have seen it, it cannot be minimised while a 10,000-track import
	 * runs, and it has nowhere to put an assistant. The steps below are the
	 * SAME steps -- same store, same endpoints, same refusal rules -- drawn
	 * OVER the performance view rather than instead of it.
	 *
	 * THE FAILURE STATES ARE THE POINT. Reported by a tester on a MacBook Air
	 * whose rekordbox library was right there: the detect step read as "failed
	 * to find rekordbox library", in grey, with nothing enabled to press next.
	 * So, in this component:
	 *   - every fatal sentence is `role="alert"` in var(--danger), and the
	 *     probe line that is the REASON is red too (see $lib/setup/detect-view);
	 *   - the reason Continue is refused renders INLINE beside the buttons,
	 *     never only in a hover title;
	 *   - three escape actions (look again / choose a folder / continue without
	 *     importing) are ALWAYS enabled, so no detection result dead-ends;
	 *   - a detection that has not answered draws a labelled scanning state
	 *     that cannot be mistaken for a verdict.
	 *
	 * AGENT-NATIVE PARITY. Every control here is one of the five setup
	 * endpoints in $lib/setup/setup-api; the only browser-only state is
	 * whether this tab is drawing the panel, the chip, or neither.
	 */
	import { goto } from '$app/navigation';
	import AssistantSidebar from '$lib/components/assistant/AssistantSidebar.svelte';
	import StemsPrompt from '$lib/components/rb/StemsPrompt.svelte';
	import { capabilities } from '$lib/api/capabilities.svelte';
	import { jobsStore, errorTail } from '$lib/rb/jobs-store.svelte';
	import {
		type AnalysisQueue,
		getAnalysisQueue,
		startAnalysisQueueDrain
	} from '$lib/rb/api-ingest';
	import { analysisFollowup } from '$lib/setup/analysis-followup';
	import { stemsJobFeedback } from '$lib/setup/stems-feedback';
	// StemsPrompt paints from the --rb-* palette, which theme.css scopes under
	// .perf-root on purpose so it cannot leak into the app's own accent. Its
	// mount below is wrapped in that class; without this import every colour
	// var it reads would be undefined.
	import '$lib/rb/theme.css';
	import {
		ESCAPE_ACTIONS,
		SCANNING_SENTENCE,
		blockerTone,
		detectPhase,
		escapeAgentEndpoint,
		probeRows,
		shortenPath
	} from '$lib/setup/detect-view';
	import {
		clearSetupIncomplete,
		closeSetupOverlay,
		collapseSetupOverlay,
		expandSetupOverlay,
		openSetupOverlay,
		setupOverlay
	} from '$lib/setup/overlay.svelte';
	import { SETUP_HOST_ROUTE } from '$lib/setup/run-setup';
	import { nativeShellKind, pickFolder } from '$lib/shell/native-shell';
	import {
		FOLDER_STAGE_LABELS,
		STAGE_LABELS,
		accessCaveat,
		agentAccessDetail,
		blockerAgentDetail,
		blockerSentence,
		finalSetupRefusal,
		finalSetupRefusalAgent,
		folderVerdict,
		setupProbePending
	} from '$lib/setup/setup-api';
	import {
		AGENT_DETAILS_LABEL,
		humanDataDirLabel,
		humanImportFailure,
		humanImportJobMessage,
		humanImportJobStatus,
		humanImportSourceLabel,
		humanSetupProbePending,
		humanStageLabel,
		humanStemsFailed
	} from '$lib/setup/present';
	import {
		STEP_TITLES,
		advanceRefusal,
		backRefusal,
		fatalBlockers,
		humanRefusal,
		importPct,
		setupWizard,
		stepCount,
		stepPosition,
		visibleSteps
	} from '$lib/setup/wizard.svelte';

	/** Only a FINAL refusal (a legacy daemon that has no /api/v1/setup) makes
	 * this surface inert. An unfinished health probe is a scanning state, not
	 * a verdict -- rendering it as one is what told a first-run user their
	 * setup had failed one tick after load. */
	const refusal = $derived(finalSetupRefusal());
	const probing = $derived(setupProbePending());
	const step = $derived(setupWizard.step);
	const detection = $derived(setupWizard.detection);
	const status = $derived(setupWizard.status);
	const detectState = $derived(setupWizard.detectState);

	/** The job row, straight out of the jobs store. Never a local copy: the
	 * store is fed by the engine's events bus and duplicating the row here
	 * would give the overlay two truths about one import. */
	const job = $derived(
		setupWizard.jobId === null
			? null
			: (jobsStore.jobs.find((row) => row.id === setupWizard.jobId) ?? null)
	);

	const lastImport = $derived(status?.last_import ?? null);
	/** Folders macOS refused during the LAST import, whichever kind it was. */
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
	const folderRows = $derived(setupWizard.folderRows);
	/** Raw refusal: gates the button and rides on data-agent-refusal. */
	const nextRefusalAgent = $derived(
		advanceRefusal(step, { source, detection, folderRows, job })
	);
	/** Operator-safe sentence for the same refusal, the only one rendered. */
	const nextRefusal = $derived(humanRefusal(step, { source, detection, folderRows, job }));
	/** Why Back is refused here, or null. Same function that gates the button,
	 * so the tooltip and the disabled state can never disagree. */
	const backWhy = $derived(backRefusal(step, { source, detection, folderRows, job }));
	const showAddFolderRow = $derived(
		folderRows.some((row) => row.path.trim() !== '')
	);
	const importableFolderCount = $derived(setupWizard.importableFolderPaths().length);
	/** The route THIS branch walks; the folder import never visits 'confirm'. */
	const route = $derived(visibleSteps(source));
	const position = $derived(stepPosition(step, source));
	const total = $derived(stepCount(source));
	const pct = $derived(importPct(job));
	const stageLabels = $derived(source === 'folder' ? FOLDER_STAGE_LABELS : STAGE_LABELS);
	const stageNames = $derived((source === 'folder' ? status?.folder_stages : status?.stages) ?? []);
	const phase = $derived(
		detectPhase(detection, detectState === 'scanning', detectState === 'failed')
	);
	const rows = $derived(detection === null ? [] : probeRows(detection));
	/** Live while the panel is minimised, so the chip is never a lie. */
	const importRunning = $derived(
		job !== null && !['succeeded', 'failed', 'cancelled', 'unknown'].includes(job.status)
	);
	const emptyTracks = $derived(status?.tracks ?? 0);

	let refreshDecrypt = $state(false);

	/** Either desktop shell (Tauri or Electron) offers a native picker; a tab does not. */
	function canUseNativeFolderPicker(): boolean {
		return nativeShellKind() !== null;
	}

	const nativeFolderPicker = canUseNativeFolderPicker();

	/** Open the OS-native directory picker in the desktop shell; browser tabs keep
	 * the text field as the only path and this handler is never called there. */
	async function chooseFolder(rowId: string): Promise<void> {
		if (!canUseNativeFolderPicker()) return;
		try {
			const selected = await pickFolder('Choose a folder');
			if (typeof selected === 'string') {
				setupWizard.folderRows = setupWizard.folderRows.map((row) =>
					row.id === rowId ? { ...row, path: selected, scan: null } : row
				);
			}
		} catch {
			// A failed plugin load or cancelled dialog must not clear a typed path.
		}
	}
	/** The separation job StemsPrompt started, if the tester said yes. Held
	 * so BOTH the stems step and the done screen can name it: run 2 on the
	 * test Mac advanced straight past the prompt on enqueue, so the one
	 * sentence saying separation had started was drawn and left in the same
	 * tick and the tester saw no feedback at all. */
	let stemsJobId = $state<string | null>(null);

	/** The LIVE row for that job, from the jobs store the overlay already
	 * attaches. An accepted enqueue is not a started separation: on the test
	 * Mac the job failed 198 ms after its id came back. */
	const stems = $derived(
		stemsJobFeedback(
			stemsJobId,
			stemsJobId === null ? null : (jobsStore.jobs.find((row) => row.id === stemsJobId) ?? null)
		)
	);

	/**
	 * The live analyze-on-import queue, polled only on the done screen.
	 *
	 * A FOLDER import has no rekordbox database behind it, so there is no ANLZ
	 * anywhere to read a BPM, key or beatgrid from: own analysis is the only
	 * source there is. The daemon's reconcile loop does drain this queue by
	 * itself, but on a 60s timer, so the wizard asks for the drain now rather
	 * than handing over a library that reads as un-analysed.
	 */
	let analysisQueue = $state<AnalysisQueue | null>(null);
	let analysisReadError = $state<string | null>(null);
	let drainRequested = false;
	const ANALYSIS_POLL_MS = 2000;

	const analysis = $derived(analysisFollowup(analysisQueue));

	/** True only where own analysis is the ONLY source: a folder import. A
	 * rekordbox import has its own analyses line on this screen already. */
	const showAnalysis = $derived(
		step === 'done' && lastImport !== null && lastImport.kind === 'folder'
	);

	$effect(() => {
		if (!showAnalysis) return;
		let stopped = false;
		let timer: ReturnType<typeof setTimeout> | undefined;
		async function tick(): Promise<void> {
			try {
				const next = await getAnalysisQueue(1);
				if (stopped) return;
				analysisQueue = next;
				analysisReadError = null;
				// One request, once. A drain that fails must not be relaunched
				// every two seconds by a screen nobody is watching.
				if (!drainRequested && analysisFollowup(next).shouldStartDrain) {
					drainRequested = true;
					await startAnalysisQueueDrain();
				}
			} catch (exc) {
				// Never a silent empty state: a queue that cannot be read is a
				// stated unknown, not a library that needs nothing.
				if (!stopped) analysisReadError = exc instanceof Error ? exc.message : String(exc);
			}
			if (!stopped) timer = setTimeout(() => void tick(), ANALYSIS_POLL_MS);
		}
		void tick();
		return () => {
			stopped = true;
			if (timer !== undefined) clearTimeout(timer);
		};
	});

	/**
	 * Load, and RE-load if the daemon was not identified the first time.
	 *
	 * Keyed on `capabilities.flavor` deliberately. The overlay can mount in
	 * the same tick the root layout fires its one health GET, and in the
	 * packaged app the engine may not be listening yet at all. Previously
	 * nothing re-ran the load, so the step sat on a grey in-flight sentence
	 * forever. This is a state change, not a poll: it fires when the flavor
	 * resolves and when the panel is re-opened, and never otherwise.
	 */
	$effect(() => {
		if (!setupOverlay.open) return;
		void capabilities.flavor;
		void setupWizard.ensureLoaded();
	});

	/** Done reads `status.last_import`, and `status` was read when the overlay
	 * opened, before the import ran. Hand the live row to the store, which
	 * re-reads status once when this wizard's own job settles. */
	$effect(() => {
		void setupWizard.refreshStatusAfterImport(job);
	});

	/** The jobs store is the progress feed. Attached only while the overlay is
	 * open and only when the daemon offers the jobs API, so a legacy boot
	 * never opens a socket it cannot use. Attached for the whole overlay (not
	 * just the progress step) because the collapsed chip reads the same row. */
	$effect(() => {
		if (!setupOverlay.open) return;
		if (refusal !== null) return;
		if (!capabilities.jobs) return;
		return jobsStore.attach();
	});

	async function dismissAndClose(): Promise<void> {
		// finish(), not skip(): it re-reads preflight before saying the close
		// will stick. See the store for the reopen this prevents.
		if (!(await setupWizard.finish())) return;
		// Honest close: the library really is whatever was already in it, and
		// the chip that replaces the panel says so rather than vanishing.
		closeSetupOverlay({ incomplete: (setupWizard.status?.tracks ?? 0) === 0 });
	}

	async function finish(): Promise<void> {
		if (!(await setupWizard.finish())) return;
		closeSetupOverlay();
	}

	function reopen(): void {
		openSetupOverlay();
		void goto(SETUP_HOST_ROUTE);
	}

	function onPanelKeydown(event: KeyboardEvent): void {
		// Escape MINIMISES rather than closes. Closing would either drop a
		// running import off screen with no trace, or record a dismissal the
		// operator never asked for; the chip does neither.
		if (event.key !== 'Escape') return;
		event.preventDefault();
		event.stopPropagation();
		collapseSetupOverlay();
	}
</script>

{#snippet backButton()}
	<!-- Back is RENDERED on every step, including the ones that refuse it.
	     A control that vanishes teaches the user nothing; a disabled one with
	     the reason on it teaches them why. -->
	<button
		type="button"
		class="secondary"
		onclick={() => setupWizard.back(job)}
		disabled={backWhy !== null || setupWizard.busy}
		title={backWhy ?? `Go back to ${STEP_TITLES[route[Math.max(position - 2, 0)]]}`}
	>
		Back
	</button>
{/snippet}

{#if setupOverlay.open && !setupOverlay.collapsed}
	<div class="su-backdrop" role="presentation">
		<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
		<div
			class="su-panel"
			role="dialog"
			aria-modal="true"
			aria-label="First-run setup"
			tabindex="-1"
			onkeydown={onPanelKeydown}
		>
			<header class="su-head">
				<h2>First-run setup</h2>
				<div class="su-head-actions">
					<button
						type="button"
						class="su-min"
						onclick={() => collapseSetupOverlay()}
						title="Minimise setup to a chip. The import keeps running and this step is kept."
					>
						Minimise
					</button>
				</div>
			</header>

			<div class="su-body">
				<section class="su-main" aria-label="Setup steps">
					<!-- The breadcrumb is NAVIGATION, not decoration: a step already
					     visited is a button that goes back to it. It lists only the
					     steps this branch will actually visit, so the folder import
					     never shows a rekordbox confirmation step it will skip. -->
					<ol class="steps">
						{#each route as name, index (name)}
							<li
								class="step"
								class:current={name === step}
								class:past={index < position - 1}
								aria-current={name === step ? 'step' : undefined}
							>
								{#if index < position - 1}
									<button
										type="button"
										class="step-link"
										onclick={() => setupWizard.goTo(name)}
										disabled={backWhy !== null}
										title={backWhy ?? `Go back to step ${index + 1} of ${total}: ${STEP_TITLES[name]}`}
									>
										{STEP_TITLES[name]}
									</button>
								{:else}
									<span title={`Step ${index + 1} of ${total}`}>{STEP_TITLES[name]}</span>
								{/if}
							</li>
						{/each}
					</ol>
					<p class="step-counter" role="status">
						Step {position} of {total}: {STEP_TITLES[step]}
					</p>

					{#if refusal !== null}
						<p class="fatal" role="alert">{refusal}</p>
						{#if finalSetupRefusalAgent() !== null}
							<details class="agent-details">
								<summary>{AGENT_DETAILS_LABEL}</summary>
								<pre data-agent-refusal={finalSetupRefusalAgent()}>{finalSetupRefusalAgent()}</pre>
							</details>
						{/if}
					{:else if probing}
						<p class="scanning" role="status">{humanSetupProbePending()}</p>
					{/if}

					{#if setupWizard.error !== null}
						<p class="fatal" role="alert">{setupWizard.error}</p>
						{#if setupWizard.errorDiagnostic !== null && setupWizard.errorDiagnostic !== setupWizard.error}
							<details class="agent-details">
								<summary>{AGENT_DETAILS_LABEL}</summary>
								<pre data-agent-error={setupWizard.errorDiagnostic}>{setupWizard.errorDiagnostic}</pre>
							</details>
						{/if}
					{/if}

					<!-- -------------------------------------------------- welcome -->
					{#if step === 'welcome'}
						<div class="panel">
							<p>
								This app keeps its own library. You can fill it from your existing
								DJ collection or from a folder of audio files, but only when you
								choose to start that import. Your original collection is never
								changed -- the import only reads a copy.
							</p>
							{#if status !== null}
								<p class="counts">
									Library right now:
									<strong title="Tracks currently in your library">
										{status.tracks} tracks
									</strong>,
									<strong title="Playlists currently in your library">
										{status.playlists} playlists
									</strong>.
									{#if status.data_dir}
										Stored in <span data-agent-data-dir={status.data_dir}>{humanDataDirLabel(status.data_dir)}</span>.
									{/if}
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
								{@render backButton()}
								<button
									type="button"
									onclick={() => setupWizard.next()}
									disabled={setupWizard.busy}
								>
									Get started
								</button>
								<button
									type="button"
									class="secondary"
									onclick={() => void dismissAndClose()}
									disabled={setupWizard.busy}
									title="Close setup and use the app with whatever is already in the library."
									data-agent-endpoint="POST /api/v1/setup/dismiss"
								>
									Skip for now
								</button>
							</div>
						</div>
					{/if}

					<!-- --------------------------------------------------- detect -->
					{#if step === 'detect'}
						<div class="panel">
							{#if deniedRoots.length > 0}
								<p class="fatal" role="alert">{caveat}</p>
								<p class="muted">{permissions?.how_to_grant}</p>
								{#if agentAccessDetail(permissions) !== null}
									<details class="agent-details">
										<summary>{AGENT_DETAILS_LABEL}</summary>
										<pre data-agent-denied={agentAccessDetail(permissions)}>
											{agentAccessDetail(permissions)}
										</pre>
									</details>
								{/if}
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

					{#if step === 'detect' && source === null}
						<div class="panel">
							<p class="muted">
								Choose an import source above. Nothing is imported until you pick
								one and explicitly start it.
							</p>
							<div class="actions">
								{@render backButton()}
								<button
									type="button"
									class="secondary"
									onclick={() => setupWizard.redetect()}
									title={ESCAPE_ACTIONS[0].title}
									data-agent-endpoint={escapeAgentEndpoint('redetect')}
								>
									{ESCAPE_ACTIONS[0].label}
								</button>
								<button
									type="button"
									class="secondary"
									onclick={() => setupWizard.useSource('folder')}
									title={ESCAPE_ACTIONS[1].title}
									data-agent-endpoint={escapeAgentEndpoint('folder')}
								>
									{ESCAPE_ACTIONS[1].label}
								</button>
								<button
									type="button"
									class="secondary"
									onclick={() => void dismissAndClose()}
									disabled={setupWizard.busy}
									title={ESCAPE_ACTIONS[2].title}
									data-agent-endpoint={escapeAgentEndpoint('dismiss')}
								>
									{ESCAPE_ACTIONS[2].label}
								</button>
								<button
									type="button"
									onclick={() => setupWizard.next()}
									disabled={nextRefusalAgent !== null || setupWizard.busy}
									data-agent-refusal={nextRefusalAgent}
									title={nextRefusal ?? 'Continue to the import'}
								>
									Continue
								</button>
							</div>
							{#if nextRefusal !== null}
								<p class="why" role="status">
									Continue is not available. {nextRefusal}
								</p>
							{/if}
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
							{#if setupWizard.folderCandidatesState === 'loading'}
								<p class="status-line muted" role="status">
									Looking for your music folders...
								</p>
							{/if}
							{#if (setupWizard.folderCandidates ?? []).length > 0}
								<div class="folder-suggestions">
									{#each setupWizard.folderCandidates ?? [] as candidate (candidate.path)}
										{#if candidate.readable}
											<button
												type="button"
												class="folder-chip"
												onclick={() => setupWizard.applyFolderSuggestion(candidate.path)}
												disabled={setupWizard.busy || refusal !== null}
												title="Use {shortenPath(candidate.path)}"
											>
												{shortenPath(candidate.path)}
											</button>
										{:else}
											<span
												class="folder-chip refused"
												title="This folder cannot be read yet"
												data-agent-detail={candidate.detail}
												data-agent-path={candidate.path}
											>
												{shortenPath(candidate.path)}
											</span>
										{/if}
									{/each}
								</div>
							{/if}
							<div class="folder-rows">
								{#each folderRows as row (row.id)}
									<div class="folder-row">
										<form
											class="folder-form"
											onsubmit={(event) => event.preventDefault()}
										>
											<div class="folder-path-row">
												<input
													type="text"
													placeholder="/Users/you/Music"
													bind:value={row.path}
													aria-label="Folder to import"
												/>
												<button
													type="button"
													class="folder-pick"
													onclick={() => void chooseFolder(row.id)}
													disabled={setupWizard.busy ||
														refusal !== null ||
														!nativeFolderPicker}
													title={nativeFolderPicker
														? 'Choose a folder'
														: 'Choose a folder (available in the Open DJ desktop app)'}
													aria-label="Choose a folder"
												>
													<svg
														class="folder-pick-icon"
														width="16"
														height="16"
														viewBox="0 0 16 16"
														aria-hidden="true"
														focusable="false"
													>
														<path
															d="M1.5 3.25A1.25 1.25 0 0 1 2.75 2h3.086a1.25 1.25 0 0 1 .884.366l.78.78A1.25 1.25 0 0 1 8.164 3.5H13.25A1.25 1.25 0 0 1 14.5 4.75v7.5A1.25 1.25 0 0 1 13.25 13.5H2.75A1.25 1.25 0 0 1 1.5 12.25v-9Z"
															fill="currentColor"
														/>
													</svg>
												</button>
											</div>
											<div class="folder-row-actions">
												<button
													type="submit"
													onclick={() => setupWizard.checkFolderRow(row.id)}
													disabled={setupWizard.busy || refusal !== null}
													title={refusal ??
														'Look inside this folder without importing it'}
												>
													Check this folder
												</button>
												{#if folderRows.length > 1}
													<button
														type="button"
														class="secondary folder-remove"
														onclick={() => setupWizard.removeFolderRow(row.id)}
														disabled={setupWizard.busy || refusal !== null}
														aria-label="Remove folder"
														title="Remove this folder row"
													>
														Remove
													</button>
												{/if}
											</div>
										</form>

										{#if row.scan !== null}
											{#if row.scan.denied}
												<p class="fatal" role="alert">{folderVerdict(row.scan)}</p>
											{:else}
												<p class="verdict" role="status">{folderVerdict(row.scan)}</p>
											{/if}
											{#if row.scan.readable}
												<p class="counts">
													<strong
														title="Readable audio files found under this folder. iCloud placeholders are counted separately and never opened."
													>
														{row.scan.audio_files} audio files
													</strong>
													{#if row.scan.icloud_placeholders > 0}
														,
														<strong
															title="Files that exist but whose bytes live in iCloud. They are skipped, never downloaded."
														>
															{row.scan.icloud_placeholders} iCloud placeholders skipped
														</strong>
													{/if}
												</p>
												<ul class="probes">
													{#each row.scan.sample as example (example)}
														<li><code>{shortenPath(example)}</code></li>
													{/each}
												</ul>
											{/if}
										{/if}
									</div>
								{/each}
							</div>
							{#if showAddFolderRow}
								<button
									type="button"
									class="secondary folder-add"
									onclick={() => setupWizard.addFolderRow()}
									disabled={setupWizard.busy || refusal !== null}
									aria-label="Add another folder"
									title="Add another music folder"
								>
									+
								</button>
							{/if}

							<div class="actions">
								{@render backButton()}
								<button
									type="button"
									class="secondary"
									onclick={() => setupWizard.useSource('rekordbox')}
									title="Go back to looking for a rekordbox collection"
								>
									Look for rekordbox instead
								</button>
								<button
									type="button"
									class="secondary"
									onclick={() => void dismissAndClose()}
									disabled={setupWizard.busy}
									title={ESCAPE_ACTIONS[2].title}
									data-agent-endpoint={escapeAgentEndpoint('dismiss')}
								>
									{ESCAPE_ACTIONS[2].label}
								</button>
								<button
									type="button"
									onclick={() => setupWizard.beginFolderImport()}
									disabled={nextRefusalAgent !== null || setupWizard.busy}
									data-agent-refusal={nextRefusalAgent}
									title={nextRefusal ??
										(importableFolderCount > 1
											? 'Import these folders'
											: 'Import this folder')}
								>
									{importableFolderCount > 1
										? 'Import these folders'
										: 'Import this folder'}
								</button>
							</div>
							{#if nextRefusal !== null}
								<p class="why" role="status">
									Import is not available yet. {nextRefusal}
								</p>
							{/if}
						</div>
					{/if}

					{#if step === 'detect' && source === 'rekordbox'}
						<div class="panel">
							<h3>What is on this machine</h3>
							{#if phase === 'scanning'}
								<p class="scanning" role="status">{SCANNING_SENTENCE}</p>
							{:else if phase === 'failed'}
								<p class="muted" role="status" data-agent-detect-state="failed">
									The search for your music did not finish. Look again, or import a
									folder instead.
								</p>
							{:else if detection !== null}
								<ul class="probes">
									{#each rows as row (row.key)}
										<li
											class:danger={row.danger}
											title={row.title}
											data-agent-detail={row.agentDetail}
										>
											{#if row.danger}
												<span role="alert">{row.text}</span>
											{:else}
												{row.text}
											{/if}
										</li>
									{/each}
								</ul>

								{#if detection.import_source !== null}
									<p>{humanImportSourceLabel(detection.import_source_encrypted)}</p>
									<details class="agent-details">
										<summary>{AGENT_DETAILS_LABEL}</summary>
										<pre data-agent-import-source={detection.import_source}>
											{detection.import_source}
										</pre>
									</details>
								{/if}

								{#if detection.rekordbox_running}
									<p class="muted">
										Your DJ app is running. That is fine -- the import reads a copy
										-- but anything you change there from now on will not be in it.
									</p>
								{/if}

								{#each blockers as code (code)}
									{#if blockerTone(code) === 'danger'}
										<p class="fatal" role="alert">{blockerSentence(code, detection)}</p>
									{:else}
										<p class="warning" role="status">
											{blockerSentence(code, detection)}
										</p>
									{/if}
									<details class="agent-details">
										<summary>{AGENT_DETAILS_LABEL}</summary>
										<pre data-agent-blocker={code}>{blockerAgentDetail(code, detection)}</pre>
									</details>
								{/each}
							{/if}

							<!-- ALWAYS-AVAILABLE PATHS. None of these is gated on what
							     detection said, which is the entire fix: there is no
							     result that leaves this step with nothing to press. -->
							<div class="actions">
								{@render backButton()}
								<button
									type="button"
									class="secondary"
									onclick={() => setupWizard.redetect()}
									title={ESCAPE_ACTIONS[0].title}
									data-agent-endpoint={escapeAgentEndpoint('redetect')}
								>
									{ESCAPE_ACTIONS[0].label}
								</button>
								<button
									type="button"
									class="secondary"
									onclick={() => setupWizard.useSource('folder')}
									title={ESCAPE_ACTIONS[1].title}
									data-agent-endpoint={escapeAgentEndpoint('folder')}
								>
									{ESCAPE_ACTIONS[1].label}
								</button>
								<button
									type="button"
									class="secondary"
									onclick={() => void dismissAndClose()}
									disabled={setupWizard.busy}
									title={ESCAPE_ACTIONS[2].title}
									data-agent-endpoint={escapeAgentEndpoint('dismiss')}
								>
									{ESCAPE_ACTIONS[2].label}
								</button>
								<button
									type="button"
									onclick={() => setupWizard.next()}
									disabled={nextRefusalAgent !== null || setupWizard.busy}
									title={nextRefusal ?? 'Continue to the import'}
								>
									Continue
								</button>
							</div>
							{#if nextRefusal !== null}
								<p class="why" class:fatal={fatal.length > 0} role={fatal.length > 0 ? 'alert' : 'status'}>
									Continue is not available. {nextRefusal}
									{#if fatal.length > 0}
										Use one of the three options above instead.
									{/if}
								</p>
								{#if nextRefusalAgent !== null}
									<details class="agent-details">
										<summary>{AGENT_DETAILS_LABEL}</summary>
										<pre data-agent-refusal={nextRefusalAgent}>{nextRefusalAgent}</pre>
									</details>
								{/if}
							{/if}
						</div>
					{/if}

					<!-- -------------------------------------------------- confirm -->
					{#if step === 'confirm'}
						<div class="panel">
							<h3>Confirm the import</h3>
							{#if detection !== null && detection.import_source !== null}
								<p>
									Reading your collection into
									<span data-agent-data-dir={status?.data_dir ?? ''}>{status?.data_dir ? humanDataDirLabel(status.data_dir) : 'your library'}</span>.
								</p>
							{/if}
							<p>These are the stages it will report:</p>
							<ol class="stages">
								{#each stageNames as stage (stage)}
									<li data-agent-stage={stage}>{humanStageLabel(stage, stageLabels)}</li>
								{/each}
							</ol>
							{#if detection?.plain_copy.exists}
								<label class="checkbox">
									<input type="checkbox" bind:checked={refreshDecrypt} />
									Unlock the collection again instead of reusing the saved copy
								</label>
							{/if}
							<div class="actions">
								{@render backButton()}
								<!-- #3422: Confirm was the one step whose only exit was Back. -->
								<button
									type="button"
									class="secondary"
									onclick={() => void dismissAndClose()}
									disabled={setupWizard.busy}
									title={ESCAPE_ACTIONS[2].title}
									data-agent-endpoint={escapeAgentEndpoint('dismiss')}
								>
									{ESCAPE_ACTIONS[2].label}
								</button>
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

					<!-- ------------------------------------------------- progress -->
					{#if step === 'progress'}
						<div class="panel">
							<h3>Importing</h3>
							{#if job === null}
								<p class="scanning" role="status">
									Waiting for the import to start. Nothing has come back yet.
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
									<span title="Current import status">
										{humanImportJobStatus(job.status)}
									</span>
									{#if humanImportJobMessage(job.message, job.status) !== null}
										-- {humanImportJobMessage(job.message, job.status)}
									{/if}
								</p>
								<details class="agent-details">
									<summary>{AGENT_DETAILS_LABEL}</summary>
									<pre
										data-agent-job-status={job.status}
										data-agent-job-message={job.message ?? ''}
									>
status={job.status}
{#if job.message !== null && job.message !== undefined}
message={job.message}
{/if}
									</pre>
								</details>
								{#if job.error !== null && job.error !== undefined}
									<p class="fatal" role="alert">{humanImportFailure(job.error)}</p>
									<details class="agent-details">
										<summary>{AGENT_DETAILS_LABEL}</summary>
										<pre class="error-tail" data-agent-job-error={setupWizard.jobId}>
											{errorTail(job.error)}
										</pre>
									</details>
								{/if}
							{/if}
							<div class="actions">
								{@render backButton()}
								<button
									type="button"
									class="secondary"
									onclick={() => collapseSetupOverlay()}
									title="Minimise setup to a chip and use the app while the import runs."
								>
									Use the app while this runs
								</button>
								<button
									type="button"
									onclick={() => setupWizard.next()}
									disabled={nextRefusalAgent !== null}
									title={nextRefusal ?? 'Continue'}
								>
									Continue
								</button>
							</div>
							{#if nextRefusal !== null}
								<p class="why" role="status">Continue is not available. {nextRefusal}</p>
							{/if}
						</div>
					{/if}

					<!-- ---------------------------------------------------- stems -->
					{#if step === 'stems'}
						<div class="panel">
							<h3>Stems analysis</h3>
							<p>
								Stem separation splits a track into vocals, drums, bass and the
								rest. It is what makes acapella and instrumental playback possible.
							</p>

							<!--
								MOUNTED, not rebuilt. StemsPrompt is af--stems-modal's component
								and the props below are the entire contract between the two
								lanes. It refuses to ask the question until GET
								/api/v1/stems/plan has told it how many tracks, how long and how
								much, so this wizard deliberately pre-fetches nothing and passes
								no numbers in -- a second source for those figures is a second
								thing that can disagree with the run.
							-->
							<div class="perf-root stems-mount">
								<!--
									NO auto-advance on enqueue. It used to call next() in the
									same tick the job id arrived, so the one screen that said
									separation had started was drawn and left together and the
									tester saw the wizard jump with no feedback at all
									(test Mac, run 2, Wed 16 Sep 2026). Skipping still
									advances: there is nothing to report about a no.
								-->
								<StemsPrompt
									onenqueued={(jobId) => {
										stemsJobId = jobId;
									}}
									onskip={() => setupWizard.next()}
								/>
							</div>

							{#if stems.state === 'failed' || stems.state === 'unknown'}
								<p class="fatal" role="alert" title="Stem separation" data-agent-job={stemsJobId}>
									{humanStemsFailed()}
								</p>
								<details class="agent-details">
									<summary>{AGENT_DETAILS_LABEL}</summary>
									<pre data-agent-stems={stems.message}>{stems.message}</pre>
								</details>
							{:else if stems.state !== 'none'}
								<p class="started" role="status" title="Stem separation" data-agent-job={stemsJobId}>
									{stems.message} You can press Continue whenever you like.
								</p>
							{/if}

							<div class="actions">
								{@render backButton()}
								<button type="button" onclick={() => setupWizard.next()}>Continue</button>
							</div>
						</div>
					{/if}

					<!-- ----------------------------------------------------- done -->
					{#if step === 'done'}
						<div class="panel">
							<h3>Done</h3>
							{#if lastImport !== null && lastImport.kind === 'rekordbox'}
								<p class="counts">
									Imported
									<strong title="Tracks added to your Open DJ library">
										{lastImport.tracks} tracks
									</strong>
									and
									<strong title="Playlists added to your Open DJ library">
										{lastImport.playlists} playlists
									</strong>.
								</p>
								<p class="muted">
									<span
										title="Analysis files (waveforms, beatgrids) found on disk, out of the imported tracks that name one"
									>
										{lastImport.analyses_linked} of
										{lastImport.analyses_expected}
									</span>
									analyses were found for your waveform data.
									{#if lastImport.analyses_linked === 0 && lastImport.analyses_expected > 0}
										None resolved, so waveforms will not draw until that folder is
										reachable.
									{/if}
								</p>
								{#if importDenied.length > 0}
									<p class="fatal" role="alert">
										Some music folders could not be read during this import, so the
										counts above cover only what could be accessed.
									</p>
									<details class="agent-details">
										<summary>{AGENT_DETAILS_LABEL}</summary>
										<pre data-agent-denied={importDenied.join(', ')}>{importDenied.join(', ')}</pre>
									</details>
								{/if}
							{:else if lastImport !== null && lastImport.kind === 'folder'}
								<p class="counts">
									Imported
									<strong title="Tracks added to your Open DJ library">
										{lastImport.tracks_written} tracks
									</strong>
									from
									<strong title="Readable audio files found under the folders you chose">
										{lastImport.files_seen} readable audio files
									</strong>.
								</p>
								<!--
									The import itself reads tags only, which used to be the
									whole sentence and read as a dead end. It is only half:
									own analysis then supplies the BPM, key and beatgrid,
									and this reads the live queue rather than the frozen
									import record so the number moves.
								-->
								{#if analysisReadError !== null}
									<p class="fatal" role="alert" title="Analysis status could not be read">
										Could not read what still needs analyzing, so the state of
										these tracks is unknown.
									</p>
									<details class="agent-details">
										<summary>{AGENT_DETAILS_LABEL}</summary>
										<pre data-agent-analysis-error={analysisReadError}>{analysisReadError}</pre>
									</details>
								{:else if analysis.state === 'failed'}
									<p class="fatal" role="alert" title="Analysis did not finish">
										Open DJ could not work out the BPM, key and beatgrid for these
										tracks, so they have none yet.
									</p>
									<details class="agent-details">
										<summary>{AGENT_DETAILS_LABEL}</summary>
										<pre data-agent-analysis={analysis.message}>{analysis.message}</pre>
									</details>
								{:else if analysis.state === 'working'}
									<p
										class="warning"
										role="status"
										title="Tracks analyzed by Open DJ's own analysis, out of the imported tracks with no rekordbox analysis to read"
									>
										{analysis.message}
									</p>
									<progress
										max={analysis.total}
										value={analysis.analyzed}
										title={`${analysis.analyzed} of ${analysis.total} analyzed`}
									></progress>
								{:else if analysis.state === 'done'}
									<p class="muted" role="status" title={analysis.message}>
										{analysis.message}
									</p>
								{:else}
									<p class="muted" role="status">{analysis.message}</p>
								{/if}
								{#if lastImport.files_dataless > 0}
									<p class="muted">
										<span
											title="Files present but stored in iCloud with no local copy. They were skipped, never downloaded."
										>
											{lastImport.files_dataless}
										</span>
										iCloud placeholders were skipped rather than downloaded.
									</p>
								{/if}
								{#if lastImport.files_rejected_unplayable > 0}
									<p class="warning">
										<span
											title="Allowlisted files that are not playable audio. They were skipped and never became library tracks."
										>
											{lastImport.files_rejected_unplayable}
										</span>
										file(s) were skipped because they are not playable audio.
									</p>
								{/if}
								{#if importDenied.length > 0}
									<p class="fatal" role="alert">
										Some folders could not be read, so the counts above cover only
										what was accessible.
									</p>
									<details class="agent-details">
										<summary>{AGENT_DETAILS_LABEL}</summary>
										<pre data-agent-denied={importDenied.join(', ')}>{importDenied.join(', ')}</pre>
									</details>
								{/if}
							{:else}
								<p class="muted">
									No import was recorded yet. The library is whatever was already in
									it.
								</p>
							{/if}
							{#if stems.state === 'failed' || stems.state === 'unknown'}
								<p class="fatal" role="alert" title="Stem separation" data-agent-job={stemsJobId}>
									{humanStemsFailed()}
								</p>
								<details class="agent-details">
									<summary>{AGENT_DETAILS_LABEL}</summary>
									<pre data-agent-stems={stems.message}>{stems.message}</pre>
								</details>
							{:else if stems.state !== 'none'}
								<p class="muted" role="status" title="Stem separation" data-agent-job={stemsJobId}>
									{stems.message}
								</p>
							{/if}
							<div class="actions">
								{@render backButton()}
								<button type="button" onclick={() => void finish()} disabled={setupWizard.busy}>
									Start playing
								</button>
							</div>
						</div>
					{/if}
				</section>

				<!-- The assistant lane. Contract with the assistant-backend work is
				     exactly { visible }; its internals are not this component's
				     business. Visible while the panel is, so a stuck first-run user
				     has somebody to ask. -->
				<AssistantSidebar visible={true} />
			</div>
		</div>
	</div>
{:else if setupOverlay.open && setupOverlay.collapsed}
	<button
		type="button"
		class="su-chip"
		onclick={() => expandSetupOverlay()}
		title={importRunning
			? 'Setup is minimised while the import runs. Click to reopen it with live progress.'
			: 'Setup is minimised. Click to reopen it at the step you left.'}
	>
		{#if importRunning}
			<span class="su-chip-dot" aria-hidden="true"></span>
			<span title={`Import progress: ${pct}% of the stages the engine reports`}>
				Importing {pct}%
			</span>
		{:else}
			<span>Setup ({STEP_TITLES[step]})</span>
		{/if}
	</button>
{:else if setupOverlay.incomplete}
	<div class="su-incomplete" role="status">
		<span>
			Setup incomplete -- library is
			<strong title="Tracks currently in your library">
				empty ({emptyTracks} tracks)
			</strong>.
		</span>
		<button type="button" onclick={reopen} title="Re-open the first-run setup wizard.">
			Run setup
		</button>
		<button
			type="button"
			class="su-dismiss"
			onclick={() => clearSetupIncomplete()}
			title="Hide this note. Setup stays re-openable from Settings (Cmd+,) > Run setup."
			aria-label="Hide the setup incomplete note"
		>
			x
		</button>
	</div>
{/if}

<style>
	.su-backdrop {
		position: fixed;
		inset: 0;
		z-index: 380;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 3vh 1rem;
		/* Translucent on purpose: the app has to stay visible beneath the ask. */
		background: rgba(0, 0, 0, 0.55);
	}
	.su-panel {
		width: min(1080px, 96vw);
		max-height: 92vh;
		display: flex;
		flex-direction: column;
		background: var(--surface, #121720);
		border: 1px solid var(--border, #1c222c);
		border-radius: 12px;
		box-shadow: 0 18px 50px rgba(0, 0, 0, 0.5);
		color: var(--fg);
		overflow: hidden;
		outline: none;
	}
	.su-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 0.75rem 1rem;
		border-bottom: 1px solid var(--border);
	}
	.su-head h2 {
		margin: 0;
		font-size: 1.05rem;
	}
	.su-head-actions {
		display: flex;
		gap: 0.4rem;
	}
	.su-min {
		font: inherit;
		font-size: 0.8rem;
		padding: 0.25rem 0.7rem;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--fg);
		cursor: pointer;
	}
	.su-body {
		display: flex;
		min-height: 0;
		flex: 1;
	}
	.su-main {
		flex: 1 1 auto;
		min-width: 0;
		overflow: auto;
		padding: 1rem 1.25rem 1.25rem;
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
	/* A visited step is a real button. It inherits the pill's own look so the
	   breadcrumb does not turn into a row of mismatched controls. */
	.step-link {
		all: unset;
		cursor: pointer;
		font: inherit;
		color: inherit;
	}
	.step-link:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.step-link:disabled {
		cursor: not-allowed;
		opacity: 0.55;
	}
	.step-counter {
		font-size: 0.85rem;
		color: var(--muted);
		margin: -0.5rem 0 1rem;
	}
	.panel {
		border: 1px solid var(--border);
		background: var(--bg);
		border-radius: 6px;
		padding: 1rem 1.25rem;
		margin-bottom: 0.75rem;
	}
	.actions {
		display: flex;
		flex-wrap: wrap;
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
	/* SCANNING is its own look on purpose: it must not be mistakable for a
	   verdict, and it must not be mistakable for a failure. */
	.scanning {
		color: var(--accent);
		border-left: 3px solid var(--accent-dim);
		padding-left: 0.6rem;
	}
	.verdict {
		color: var(--fg);
		border-left: 3px solid var(--accent-dim);
		padding-left: 0.6rem;
	}
	.warning {
		color: var(--muted);
		border-left: 3px solid var(--accent-dim);
		padding-left: 0.6rem;
	}
	.fatal,
	.probes li.danger {
		color: var(--danger);
	}
	.fatal {
		border-left: 3px solid var(--danger);
		padding-left: 0.6rem;
		font-weight: 560;
	}
	.probes li.danger {
		font-weight: 560;
	}
	/* The inline reason a control is refused. Never only a hover title. */
	.why {
		margin-top: 0.6rem;
		font-size: 0.9rem;
		color: var(--muted);
	}
	.why.fatal {
		color: var(--danger);
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
	.started {
		margin: 8px 0 0;
		font-size: 13px;
		color: var(--ok, #4ecb8c);
	}
	.panel progress {
		display: block;
		width: 100%;
		max-width: 420px;
		height: 6px;
		margin: 6px 0 0;
	}
	.stems-mount {
		margin-top: 0.75rem;
	}
	.checkbox {
		display: block;
		margin-top: 0.75rem;
	}
	.folder-suggestions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
		margin-top: 0.75rem;
	}
	.folder-chip {
		font: inherit;
		font-size: 0.8rem;
		padding: 0.25rem 0.6rem;
		border-radius: 999px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--fg);
		cursor: pointer;
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.folder-chip:hover:not(:disabled) {
		border-color: var(--accent-dim);
	}
	.folder-chip:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.folder-chip.refused {
		color: var(--muted);
		border-color: var(--border);
		cursor: not-allowed;
		opacity: 0.7;
	}
	.folder-rows {
		display: flex;
		flex-direction: column;
		gap: 1rem;
		margin-top: 0.75rem;
	}
	.folder-row {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.folder-form {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		align-items: center;
	}
	.folder-row-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		align-items: center;
	}
	.folder-add {
		margin-top: 0.5rem;
		min-width: 2.25rem;
		font-size: 1.1rem;
		line-height: 1;
		padding: 0.2rem 0.65rem;
	}
	.folder-remove {
		font-size: 0.85rem;
	}
	.folder-path-row {
		display: flex;
		flex: 1 1 auto;
		gap: 0.35rem;
		min-width: min(100%, 18rem);
	}
	.folder-path-row input {
		flex: 1 1 auto;
		min-width: 0;
	}
	.folder-pick {
		display: grid;
		place-items: center;
		flex: 0 0 auto;
		width: 2.25rem;
		padding: 0;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--fg);
		cursor: pointer;
	}
	.folder-pick:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.folder-pick-icon {
		display: block;
	}
	.folder-form input::placeholder {
		color: var(--muted);
		opacity: 0.65;
		font-style: italic;
	}
	.footnote {
		font-size: 0.8rem;
		margin-top: 1rem;
	}
	.agent-details {
		margin-top: 0.5rem;
		font-size: 0.75rem;
		color: var(--muted);
	}
	.agent-details pre {
		white-space: pre-wrap;
		margin: 0.35rem 0 0;
		font-size: 0.72rem;
	}
	/* ----- the minimised chip and the incomplete note --------------------- */
	.su-chip,
	.su-incomplete {
		position: fixed;
		right: 1rem;
		bottom: 1.75rem;
		z-index: 370;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.4rem 0.9rem;
		font: inherit;
		font-size: 0.85rem;
		border-radius: 999px;
		border: 1px solid var(--accent-dim);
		background: var(--surface);
		color: var(--fg);
		box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45);
	}
	.su-chip {
		cursor: pointer;
	}
	.su-chip-dot {
		width: 0.5rem;
		height: 0.5rem;
		border-radius: 50%;
		background: var(--accent);
	}
	.su-incomplete {
		border-color: var(--danger);
	}
	.su-incomplete button {
		font: inherit;
		font-size: 0.8rem;
		padding: 0.15rem 0.6rem;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--fg);
		cursor: pointer;
	}
	.su-dismiss {
		border: none;
		color: var(--muted);
	}
</style>
