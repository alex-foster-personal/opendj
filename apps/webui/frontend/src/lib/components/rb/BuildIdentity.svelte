<!--
	WHAT AM I -- the build stamp, in the bottom tray, bottom right.

	This is deliberately NOT in an About panel. The failure it exists to
	prevent is somebody using a stale or dirty build for an hour without
	noticing, and a readout you have to go looking for cannot prevent that.

	WHY IT MOVED. It used to be `position: fixed` in the bottom LEFT corner,
	which meant it floated ON TOP of whatever was underneath: the sidebar on
	the app shell, the browser panel's connectivity dots on /performance. A
	permanent readout that covers other controls trades one problem for
	another. It is now an ordinary member of the bottom tray, pushed to the
	right end of it, so it occupies its own space and overlaps nothing. There
	is one mount per surface (the app shell's tray in +layout.svelte, the
	browser panel's bottom bar on /performance) and they are mutually
	exclusive, so exactly one is on screen at a time.

	Everything shown is read at RUNTIME: the engine half over HTTP, the shell
	half from the global the Tauri binary injected, the address from this
	page's own origin. There is no build-time string literal in this bundle
	and no fallback value anywhere. When a side cannot state what it is, this
	renders a visible fault, because "unknown" and "fine" must never look the
	same.

	THE ADDRESS IS PART OF THE IDENTITY. The packaged app starts its engine on
	an OS-assigned free port, so the app's URL is different every launch and is
	written down nowhere. A tester who wanted to open it in a real browser
	guessed the dev port and got nothing. The foldout now states the address,
	selectable, with a copy button.

	The whole component is one self-contained file (plus its logic module,
	$lib/rb/build-identity.ts, which the unit tests drive).
-->
<script lang="ts">
	import { onMount } from 'svelte';

	import {
		describeDrift,
		engineBaseUrl,
		explainEngineUrl,
		explainSide,
		fetchEngineBuild,
		formatAge,
		copyBuildIdentityToClipboard,
		formatStamp,
		readShellBuild,
		shortLabel,
		UNKNOWN,
		type EngineStamp,
		type EngineUrl,
		type ShellStamp,
		type SideState
	} from '$lib/rb/build-identity';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import {
		applyUpdate,
		canApplyHere,
		fetchUpdateCheck,
		isUpdaterExpected,
		summarizeUpdate,
		type ApplyProgress,
		type UpdateState
	} from '$lib/rb/update-channel';

	// The shell stamp is synchronous: it was injected before any script ran.
	const shell: SideState<ShellStamp> = readShellBuild();
	let engine = $state<SideState<EngineStamp>>({ kind: 'loading' });
	let expanded = $state(false);
	/** The outcome of the last copy attempt, success or failure, never blank. */
	let copyNote = $state<string | null>(null);

	const drift = $derived(describeDrift(shell, engine));
	const engineStamp = $derived(engine.kind === 'ok' ? formatStamp(engine.value.built_at_utc) : null);
	const shellStamp = $derived(shell.kind === 'ok' ? formatStamp(shell.value.built_at_utc) : null);
	const channel = $derived(
		shell.kind === 'ok' && shell.value.release_channel ? shell.value.release_channel : null
	);
	const evidenceStamp = $derived(
		shell.kind === 'ok' ? formatStamp(shell.value.evidence_written_at_utc) : null
	);
	/** Re-read once a minute so the age ticks over while the app stays open. */
	let now = $state(new Date());
	const engineAge = $derived(engine.kind === 'ok' ? formatAge(engine.value.built_at_utc, now) : null);

	/** Where this app lives. Read once at mount, for the same reason the build
	 * identity is: it does not change while the page is open. */
	let engineUrl = $state<EngineUrl>({ kind: 'fault', reason: 'not resolved yet' });

	// ----- the update channel ---------------------------------------------
	// Checked once at mount, UNLIKE the build identity, because the answer can
	// change while the window is open: somebody publishes a release and this
	// build stops being current without anything here changing. Re-checked only
	// on request, so an idle app is not polling a release host all night.
	let update = $state<UpdateState>({ kind: 'idle' });
	/** Progress of an install in flight, or null when none is. */
	let applying = $state<ApplyProgress | null>(null);
	/** The outcome of the last install attempt. Success or failure, never blank. */
	let applyNote = $state<string | null>(null);

	const updaterExpected = $derived(
		isUpdaterExpected({
			isDev: import.meta.env.DEV,
			engineSource: engine.kind === 'ok' ? engine.value.source : null,
			inTauri: canApplyHere()
		})
	);
	const updateSummary = $derived(summarizeUpdate(update, { updaterExpected }));
	/** Only the desktop shell has an installer. A browser tab is told so. */
	const installable = $derived(
		update.kind === 'ok' && update.value.status === 'update-available'
	);

	async function checkForUpdates(): Promise<void> {
		update = { kind: 'checking' };
		update = await fetchUpdateCheck();
	}

	/**
	 * Install and restart. Everything the updater refuses -- an unverifiable
	 * signature, a 404 endpoint, a bundle macOS will not replace -- lands in
	 * applyNote verbatim. There is no silent retry and no swallowed failure:
	 * an update that did not happen must never look like one that did.
	 */
	async function installUpdate(): Promise<void> {
		applyNote = null;
		const outcome = await applyUpdate((progress) => (applying = progress));
		applying = null;
		if (outcome.kind === 'installed') {
			applyNote = 'installed; restarting';
		} else if (outcome.kind === 'no-update') {
			applyNote = 'the updater found nothing to install; re-checking is the next step';
			void checkForUpdates();
		} else {
			applyNote = `update refused: ${outcome.reason}`;
		}
	}

	function describeApplying(progress: ApplyProgress): string {
		if (progress.phase === 'downloading') {
			const total = progress.total;
			const pct = total === null ? null : Math.round((progress.received / total) * 100);
			return pct === null ? 'downloading...' : `downloading ${pct}%`;
		} else if (progress.phase === 'checking') {
			return 'verifying the channel...';
		} else if (progress.phase === 'installing') {
			return 'installing...';
		}
		return 'restarting...';
	}

	/** The lane this artifact was built as, or the fact that it has none. */
	const lane = $derived(engine.kind === 'ok' ? (engine.value.lane_label ?? 'unlabelled') : '?');

	const driftTitle = $derived(
		drift === 'drifted'
			? 'DRIFT: the desktop shell and the engine were built from different commits. One of them is stale.'
			: drift === 'aligned'
				? 'Shell and engine were built from the same commit.'
				: 'Shell and engine cannot be compared: at least one of them did not state a commit.'
	);

	/**
	 * Copy the full build identity report. No silent success and no silent failure.
	 */
	async function copyAllDetails(): Promise<void> {
		copyNote = await copyBuildIdentityToClipboard(
			{ shell, engine, engineUrl, drift, updateSummary },
			navigator.clipboard
		);
	}

	onMount(() => {
		// One read, once. The identity of a running process does not change,
		// so polling it would be a lie dressed as freshness -- and by the
		// same argument it does not have to be read AT mount, so it is
		// deferred out of the boot burst (PERF-R6). The chip renders its
		// 'loading' face until the answer lands, exactly as it already did
		// for the duration of the request.
		bootScheduler.defer('build-identity:fetchEngineBuild', () => {
			void fetchEngineBuild().then((result) => {
				engine = result;
			});
		});
		// The address is synchronous and the tray's foldout shows it, so it
		// stays at mount.
		engineUrl = engineBaseUrl();
		// One check at startup. An indicator that only appears after somebody
		// clicks is not an indicator, and the failure this whole readout exists
		// to prevent is running a stale build without noticing.
		void checkForUpdates();
		const ageTick = setInterval(() => (now = new Date()), 60_000);
		return () => clearInterval(ageTick);
	});
</script>

<div
	class="build-identity"
	class:fault={engine.kind === 'fault' || shell.kind === 'fault'}
	class:drifted={drift === 'drifted'}
>
	<button
		type="button"
		class="summary"
		title={`This build's identity, read at runtime -- never a value compiled into this page.\n\nENGINE (serves this page): ${explainSide('engine', engine)}\n\nSHELL (the desktop window): ${explainSide('shell', shell)}\n\n${driftTitle}\n\nClick for the full stamp and this app's address.`}
		aria-expanded={expanded}
		onclick={() => (expanded = !expanded)}
	>
		<span class="lane" title="The bake-off lane this artifact was built as, from the payload manifest.">
			{lane}
		</span>
		{#if channel !== null}
			<span class="channel" title="Release channel conferred by the process, from the shell stamp.">
				{channel}
			</span>
		{/if}
		<!-- shortLabel composes the sha and the DIRTY marker, so there is one
		     place that decides what the compact label says. The span goes
		     amber as a whole when it carries DIRTY: a dirty build must not be
		     able to pass for a clean one at a glance. -->
		<span
			class="sha"
			class:dirty={engine.kind === 'ok' && engine.value.git_dirty}
			title={explainSide('engine', engine)}
		>
			{shortLabel(engine)}
		</span>
		{#if drift === 'drifted'}
			<span class="drift" title={driftTitle}>SHELL DRIFT</span>
		{/if}
		<!-- The update indicator. Only the states worth interrupting a glance
		     appear here (an available update, and a channel that could not be
		     read); "up to date" is not news and stays in the foldout. A span,
		     not a button: this sits inside the summary button already. -->
		{#if updateSummary !== null && updateSummary.prominent}
			<span class="update-badge" title={updateSummary.title}>{updateSummary.label}</span>
		{/if}
		{#if engineStamp !== null}
			<span
				class="when"
				title={`Engine built ${engineStamp.local} local time, ${engineStamp.utc} UTC (${engine.kind === 'ok' && engine.value.built_at_kind === 'payload-build' ? 'payload package time' : engine.kind === 'ok' && engine.value.built_at_kind === 'engine-start' ? 'this engine process start' : 'build stamp'}).${evidenceStamp !== null ? ` Release evidence: ${evidenceStamp.local} local, ${evidenceStamp.utc} UTC.` : ''}`}
			>
				{engineAge !== null ? `${engineAge} ago` : engineStamp.local}
			</span>
		{/if}
	</button>

	{#if expanded}
		<div class="detail-wrap">
			<button
				type="button"
				class="copy-icon"
				aria-label="Copy build identity to clipboard"
				title="Copy the full engine and shell stamp to the clipboard (same as copy all details)."
				onclick={() => void copyAllDetails()}
			>
				<svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
					<rect
						x="8"
						y="8"
						width="12"
						height="14"
						rx="1"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
					/>
					<path
						d="M6 16H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
					/>
				</svg>
			</button>
			<dl class="detail">
			<dt title="Release channel conferred on this sha, plus the evidence file timestamp for a stable install.">
				channel
			</dt>
			<dd>
				{#if channel !== null}
					<code>{channel}</code>
					{#if evidenceStamp !== null}
						<span class="meta">evidence {evidenceStamp.utc}</span>
					{/if}
				{:else}
					<span class="meta">no release channel on this stamp</span>
				{/if}
			</dd>

			<dt
				title="Version identities for the engine and the surface running this page. The surface says whether feedback came from the Chrome dev loop or a packaged DMG app."
			>
				app versioning:
			</dt>
			<dd>
				{#if engine.kind === 'ok'}
					<code>{engine.value.product_name ?? UNKNOWN}</code>
					<span class="meta">
						{engine.value.bundle_identifier ?? UNKNOWN} · v{engine.value.app_version ?? UNKNOWN}
					</span>
				{:else}
					<span class="reason">{engine.kind === 'loading' ? 'reading...' : engine.reason}</span>
				{/if}
				{#if shell.kind === 'absent'}
					<span class="meta">browser identity: Chrome dev loop</span>
				{:else if shell.kind === 'ok'}
					<span class="meta">DMG app version: v{shell.value.app_version ?? UNKNOWN}</span>
				{:else if shell.kind === 'fault'}
					<span class="reason">DMG app version unavailable: {shell.reason}</span>
				{:else}
					<span class="reason">DMG app version unavailable: reading...</span>
				{/if}
			</dd>

			<dt title="The address this app is served on. Open it in a browser to reach the same app.">
				url
			</dt>
			<dd>
				{#if engineUrl.kind === 'ok'}
					<span class="url-row">
						<a
							class="url"
							href={engineUrl.url}
							target="_blank"
							rel="noopener noreferrer"
							title={explainEngineUrl(engineUrl)}>{engineUrl.url}</a
						>
					</span>
					<span class="meta" title={explainEngineUrl(engineUrl)}>
						{engineUrl.source === 'served'
							? 'this page was served by the engine, so this is its own origin'
							: 'from VITE_API_BASE: a dev build pointed at a daemon elsewhere'}
					</span>
					{#if copyNote !== null}
						<span class="meta" class:reason={copyNote !== 'copied all details'}>{copyNote}</span>
					{/if}
				{:else}
					<span class="reason">{engineUrl.reason}</span>
				{/if}
			</dd>

			<dt
				title="The auto-update channel. Asking is plain HTTP to the engine, so it works here and in a browser; installing is the desktop shell's Tauri updater, which verifies the package signature. The same question over a CLI: python -m apps.engine_core.update_channel check"
			>
				update
			</dt>
			<dd>
				<span class="url-row">
					<button
						type="button"
						class="copy"
						onclick={() => void checkForUpdates()}
						disabled={update.kind === 'checking' || applying !== null}
						title="Ask the update channel what it is offering, now."
					>
						{update.kind === 'checking' ? 'checking...' : 'check for updates'}
					</button>
					{#if installable && canApplyHere()}
						<button
							type="button"
							class="copy install"
							onclick={() => void installUpdate()}
							disabled={applying !== null}
							title={`Download, verify and install ${update.kind === 'ok' ? update.value.available_version : ''}, then restart. The package signature is verified by the desktop shell against its compiled-in public key; a package that does not verify is refused.`}
						>
							{applying === null ? 'install and restart' : describeApplying(applying)}
						</button>
					{/if}
				</span>
				{#if updateSummary !== null}
					<span
						class="meta"
						class:reason={updateSummary.prominent}
						title={updateSummary.title}
					>
						{updateSummary.label}
					</span>
				{/if}
				{#if update.kind === 'ok' && update.value.status === 'update-available' && !canApplyHere()}
					<span
						class="meta"
						title="Applying an update replaces the installed .app bundle, which only the desktop shell can do. This page is running in a browser."
					>
						open the desktop app to install it
					</span>
				{/if}
				{#if update.kind === 'fault'}
					<span class="reason">{update.reason}</span>
				{/if}
				{#if applyNote !== null}
					<span class="meta" class:reason={applyNote !== 'installed; restarting'}>
						{applyNote}
					</span>
				{/if}
			</dd>

			<dt title="The daemon serving this page.">engine</dt>
			<dd>
				{#if engine.kind === 'ok'}
					<code>{engine.value.git_sha_full}</code>
					<span class="meta">
						{engine.value.git_branch} · {engine.value.source} · v{engine.value.engine_version}
						{#if engine.value.git_dirty}<b>· DIRTY</b>{/if}
					</span>
					{#if engineStamp !== null}
						<span class="meta">{engineStamp.local} · {engineStamp.utc} UTC</span>
					{/if}
				{:else}
					<span class="reason">{engine.kind === 'loading' ? 'reading...' : engine.reason}</span>
				{/if}
			</dd>

			<dt title="The native desktop window, if this page is running inside one.">shell</dt>
			<dd>
				{#if shell.kind === 'ok'}
					<code>{shell.value.git_sha_full}</code>
					<span class="meta">
						{shell.value.git_branch} · v{shell.value.app_version}
						{#if shell.value.git_dirty}<b>· DIRTY</b>{/if}
					</span>
					{#if shellStamp !== null}
						<span class="meta">{shellStamp.local} · {shellStamp.utc} UTC</span>
					{/if}
				{:else}
					<span class="reason">{shell.kind === 'loading' ? 'reading...' : shell.reason}</span>
				{/if}
			</dd>

			<dt class="detail-actions-label">actions</dt>
			<dd class="detail-footer">
				<button
					type="button"
					class="copy"
					onclick={() => void copyAllDetails()}
					title="Copy engine and shell identity, drift, channel, URL, and update status as plain text."
				>
					copy all details
				</button>
			</dd>
		</dl>
		</div>
	{/if}
</div>

<style>
	/* An ordinary tray citizen, NOT position: fixed. `margin-left: auto` is
	   what puts it at the right end of whichever flex tray mounts it, so the
	   tray decides the row and this decides its own end of it. */
	.build-identity {
		position: relative;
		margin-left: auto;
		z-index: 60;
		max-width: min(38rem, calc(100vw - 1rem));
		font-size: 0.68rem;
		line-height: 1.35;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		color: var(--muted, #8a8a8a);
		pointer-events: auto;
	}
	.summary {
		display: flex;
		gap: 0.4rem;
		align-items: baseline;
		padding: 0 0.35rem;
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		cursor: help;
		white-space: nowrap;
	}
	.lane {
		font-weight: 700;
		color: var(--accent, #d97757);
	}
	.channel {
		font-weight: 700;
	}
	.sha {
		letter-spacing: 0.02em;
	}
	.when {
		opacity: 0.72;
	}
	/* A dirty or drifted build must not be able to pass for a clean one at a
	   glance, so these are the only two colours in the readout. */
	.dirty,
	.drift,
	.update-badge {
		font-weight: 700;
		color: #e0b341;
	}
	/* The one control that is an ACTION rather than a readout, so it is the
	   one thing in this component that carries the accent colour. */
	.copy.install {
		color: var(--accent, #d97757);
		border-color: currentColor;
		font-weight: 700;
	}
	.copy:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.build-identity.drifted .summary,
	.build-identity.fault .summary {
		box-shadow: inset 0 0 0 1px #e0b341;
	}
	/* Opens UPWARD from the tray and is anchored to its right edge, so it
	   never resizes the tray row and never runs off the window. */
	.detail-wrap {
		position: absolute;
		right: 0;
		bottom: 100%;
		margin: 0 0 2px 0;
		z-index: 1;
	}
	.detail {
		position: relative;
		min-width: 22rem;
		max-width: min(38rem, calc(100vw - 1rem));
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 0.1rem 0.5rem;
		padding: 0.3rem 0.45rem 0.4rem;
		background: var(--bg, #1b1b1b);
		border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45);
		text-align: left;
	}
	.detail dt {
		font-weight: 700;
	}
	.detail-actions-label {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
	.copy-icon {
		position: absolute;
		top: 0.25rem;
		right: 0.35rem;
		padding: 0.1rem 0.25rem;
		border: 1px solid color-mix(in srgb, currentColor 35%, transparent);
		border-radius: 3px;
		background: var(--bg, #1b1b1b);
		color: inherit;
		cursor: pointer;
		line-height: 0;
		z-index: 1;
	}
	.detail-footer {
		grid-column: 2;
		margin: 0.25rem 0 0;
	}
	.detail dd {
		margin: 0;
		display: flex;
		flex-direction: column;
	}
	.detail code {
		word-break: break-all;
	}
	.url-row {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
	}
	.url {
		user-select: all;
		color: var(--accent, #d97757);
		text-decoration: none;
	}
	.url:hover {
		text-decoration: underline;
	}
	.copy {
		padding: 0 0.35rem;
		border: 1px solid color-mix(in srgb, currentColor 35%, transparent);
		border-radius: 3px;
		background: transparent;
		color: inherit;
		font: inherit;
		cursor: pointer;
	}
	.meta {
		opacity: 0.72;
	}
	.reason {
		color: #e0b341;
	}
</style>
