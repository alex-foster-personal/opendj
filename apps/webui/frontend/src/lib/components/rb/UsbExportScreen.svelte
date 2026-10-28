<script lang="ts">
	/**
	 * USB export screen (USBEXP-01, USBEXP-02, USBEXP-03). Opened by the
	 * rail's USB export icon and the browser bottom bar's export/eject button.
	 *
	 * Sticks: every present USB stick, each with a safe Eject.
	 * Export: plan -> review -> write -> read back, against the existing
	 * /api/v1/usb-export routes. The scope line says what it does NOT do yet.
	 * Every refusal shows the daemon's own code and sentence.
	 */
	import { onMount } from 'svelte';

	import {
		closeUsbExport,
		countUsbExportScreenMount,
		ejectUsbStick,
		exportTargets,
		followEjectNotice,
		forgetVanishedTarget,
		pickedStickGone,
		planUsbExport,
		resetUsbExportPlan,
		stickActionBlocker,
		usbExport,
		writeUsbExport
	} from '$lib/rb/usb-export.svelte';
	import { usbDiscoveryNotice, usbTracker } from '$lib/rb/usb-tracker.svelte';

	// The stick menu's Eject starts only while this screen is mounted to show
	// its progress and hold the page (Codex P1 4291127157).
	onMount(countUsbExportScreenMount);

	const sticks = $derived(exportTargets(usbTracker.volumes, usbTracker));
	const discoveryNotice = $derived(usbDiscoveryNotice(usbTracker));
	// One device operation at a time (plan, write or eject); while it runs every
	// close path is inert and the screen says what it is waiting for.
	const busy = $derived(usbExport.busy !== null);
	// Why Check plan and Write are inert: nothing picked, or the pick is not in
	// a healthy scan right now (an outage keeps the pick but proves nothing).
	const NO_TARGET = $derived(
		usbExport.targetPath === null
			? 'Pick a stick first'
			: 'The picked stick is not in a healthy USB scan right now; wait for the scan or pick again'
	);
	const target = $derived(sticks.find((s) => s.id === usbExport.targetId && s.mount_path === usbExport.targetPath) ?? null);
	// Why Check plan and Write are inert for a listed pick: no volume UUID to
	// prove the plan is for this stick (Sol P1 4398681354).
	const targetBlocker = $derived(target === null ? NO_TARGET : stickActionBlocker(target.id));

	$effect(() => {
		// A picked stick that a healthy scan no longer lists stops being a target.
		// Wait while an operation runs (it owns the plan fields); `busy` is read
		// here, so the effect re-runs when it settles and clears the stale plan
		// then, keeping what a finished write said about the stick. A discovery
		// outage proves nothing: the pick and any write result stay on screen,
		// and the actions are disabled until the scan is healthy.
		if (
			!busy &&
			usbExport.targetPath !== null &&
			pickedStickGone(usbTracker.volumes, usbTracker, usbExport.targetPath, usbExport.targetId)
		) {
			forgetVanishedTarget();
		}
	});

	// An eject notice ends when a stick plugged in after the eject is listed,
	// so it never tells the user to pull a mounted device (Codex P2 4301877384).
	$effect(() => {
		followEjectNotice(usbTracker.volumes, usbTracker);
	});

	// A modal <dialog> sits in the browser's top layer above every z-index
	// (context menus, top-bar menus), makes the rest of the page inert and keeps
	// focus inside it (Codex P1 4286889359), so nothing underneath can navigate
	// away while a stick operation runs.
	let dialogEl: HTMLDialogElement | undefined = $state();

	$effect(() => {
		const el = dialogEl;
		if (el === undefined) return;
		if (!el.open) el.showModal();
		return () => {
			if (el.open) el.close();
		};
	});

	function onCancel(e: Event): void {
		// Escape: the store decides (refused while busy), never the browser.
		e.preventDefault();
		closeUsbExport();
	}

	function onDialogClick(e: MouseEvent): void {
		// The dialog element itself is hit only outside its content box, i.e. on
		// the ::backdrop: that is the backdrop click.
		if (e.target === dialogEl) closeUsbExport();
	}

	function pick(path: string, id: string): void {
		if (usbExport.targetPath === path && usbExport.targetId === id) return;
		usbExport.targetPath = path;
		usbExport.targetId = id;
		resetUsbExportPlan();
	}
</script>

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
<dialog
	bind:this={dialogEl}
	class="ux-dialog"
	aria-label="USB export"
	data-testid="usb-export-screen"
	oncancel={onCancel}
	onclick={onDialogClick}
>
<div class="ux-screen">
	<header class="ux-head">
		<strong>USB export</strong>
		<button
			type="button"
			class="ux-x"
			onclick={closeUsbExport}
			disabled={busy}
			data-testid="usb-export-close"
			title={busy ? 'Wait for the stick operation to finish before closing' : 'Close (Esc)'}
			aria-label="Close">x</button
		>
	</header>
	{#if usbExport.busy !== null}
		<p class="ux-busy" role="status" data-testid="usb-export-busy">
			{usbExport.busy.label}... Keep the stick plugged in; this screen stays open until it finishes.
		</p>
		{#if usbExport.busyNotice !== null}
			<p class="ux-problem" data-testid="usb-export-busy-refused">{usbExport.busyNotice}</p>
		{/if}
	{/if}

	<p class="ux-scope" data-testid="usb-export-scope">
		Writes one file, <code>PIONEER/rekordbox/exportLibrary.db</code>, onto an empty stick marked
		disposable. No audio files or waveforms are copied yet, so this is not a full CDJ export.
	</p>

	<section class="ux-section" aria-label="USB sticks">
		<h3>Sticks</h3>
		{#if discoveryNotice !== null}
			<p class="ux-problem" data-testid="usb-export-discovery">{discoveryNotice}</p>
		{:else if sticks.length === 0}
			<p class="ux-dim" data-testid="usb-export-no-sticks">
				{usbTracker.scannedAt === null ? 'Looking for USB sticks...' : 'No USB stick is plugged in.'}
			</p>
		{/if}
		<ul class="ux-sticks">
			{#each sticks as stick (stick.id)}
				{@const path = stick.mount_path ?? ''}
				{@const blocker = stickActionBlocker(stick.id)}
				<li class="ux-stick" class:picked={usbExport.targetPath === path && usbExport.targetId === stick.id} data-testid="usb-export-stick">
					<label class="ux-pick" title={`Write the export to ${path}`}>
						<input
							type="radio"
							name="usb-export-target"
							checked={usbExport.targetPath === path && usbExport.targetId === stick.id}
							disabled={busy}
							onchange={() => pick(path, stick.id)}
						/>
						<span class="ux-name">{stick.name}</span>
						<span class="ux-dim ux-path">{path}</span>
					</label>
					<button
						type="button"
						class="ux-btn"
						data-testid="usb-export-eject"
						disabled={busy || blocker !== null}
						title={blocker ?? `Flush and unmount ${stick.name} so it can be pulled out`}
						onclick={() => ejectUsbStick(path, stick.id)}
					>
						{usbExport.ejecting === path ? 'Ejecting...' : 'Eject'}
					</button>
				</li>
			{/each}
		</ul>
		{#if usbExport.ejectNotice !== null}
			<p class="ux-ok" data-testid="usb-export-eject-ok">{usbExport.ejectNotice}</p>
		{/if}
		{#if usbExport.ejectProblem !== null}
			<p class="ux-problem" data-testid="usb-export-eject-problem">
				Eject refused ({usbExport.ejectProblem.code}): {usbExport.ejectProblem.message}
			</p>
		{/if}
	</section>

	<section class="ux-section" aria-label="Export">
		<h3>Export</h3>
		<label class="ux-field">
			<span title="A rekordbox-made exportLibrary.db; the playlist is built on top of a copy of it">Template database</span>
			<input
				bind:value={usbExport.templatePath}
				oninput={resetUsbExportPlan}
				disabled={busy}
				placeholder="/path/to/PIONEER/rekordbox/exportLibrary.db"
				data-testid="usb-export-template"
			/>
		</label>
		<label class="ux-field">
			<span title="Name of the playlist written onto the stick">Playlist name</span>
			<input bind:value={usbExport.playlistName} oninput={resetUsbExportPlan} disabled={busy} data-testid="usb-export-playlist" />
		</label>
		<label class="ux-field">
			<span title="Track ids from the template database, separated by commas">Track ids (from the template)</span>
			<input bind:value={usbExport.trackIdsText} oninput={resetUsbExportPlan} disabled={busy} placeholder="1, 2, 3" data-testid="usb-export-tracks" />
		</label>

		<div class="ux-actions">
			<button
				type="button"
				class="ux-btn"
				data-testid="usb-export-plan"
				disabled={busy || targetBlocker !== null}
				title={targetBlocker ?? 'Check the stick and build the plan; writes nothing'}
				onclick={planUsbExport}
			>
				{usbExport.step === 'planning' ? 'Checking...' : 'Check plan'}
			</button>
			{#if usbExport.plan !== null && usbExport.step !== 'done'}
				<button
					type="button"
					class="ux-btn ux-write"
					data-testid="usb-export-write"
					disabled={busy || targetBlocker !== null}
					title={targetBlocker ?? 'Write exactly this plan to the stick, then read it back'}
					onclick={writeUsbExport}
				>
					{usbExport.step === 'writing' ? 'Writing...' : `Write to ${usbExport.plan.volume_label}`}
				</button>
			{/if}
		</div>

		{#if usbExport.plan !== null}
			{@const plan = usbExport.plan}
			<dl class="ux-plan" data-testid="usb-export-plan-summary">
				<dt>Stick</dt>
				<dd>{plan.volume_label} <span class="ux-dim">({plan.volume_uuid})</span></dd>
				<dt>Writes</dt>
				<dd><code>{plan.output_relative_path}</code></dd>
				<dt>Playlists</dt>
				<dd title="Playlists in the plan, and how many tracks each holds">
					{plan.playlists.length === 0
						? 'none (template copied as is)'
						: plan.playlists.map((p) => `${p.name} (${p.track_ids.length})`).join(', ')}
				</dd>
				<dt>Plan</dt>
				<dd class="ux-dim" title="Content digest of this plan; the write is refused if anything changes">{plan.plan_id.slice(0, 12)}</dd>
			</dl>
		{/if}

		{#if usbExport.step === 'done' && usbExport.readback !== null}
			<p class="ux-ok" data-testid="usb-export-done">
				Written and read back from {usbExport.plan?.volume_label}: {usbExport.readback.playlists.length}
				playlist{usbExport.readback.playlists.length === 1 ? '' : 's'} verified.
			</p>
		{/if}
		{#if usbExport.problem !== null}
			<p class="ux-problem" data-testid="usb-export-problem">
				{usbExport.problem.code}: {usbExport.problem.message}
			</p>
		{/if}
	</section>
</div>
</dialog>

<style>
	.ux-busy {
		margin: 0 0 8px;
		color: var(--rb-text);
		font-size: 12px;
	}
	.ux-dialog {
		padding: 0;
		border: none;
		background: transparent;
		max-width: none;
		max-height: none;
		overflow: visible;
	}
	.ux-dialog::backdrop {
		background: rgba(0, 0, 0, 0.45);
	}
	.ux-screen {
		width: min(520px, calc(100vw - 32px));
		max-height: calc(100vh - 48px);
		overflow: auto;
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 12px 14px;
		background: var(--rb-panel-raised, #1a1f28);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 12px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
	}
	.ux-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		font-size: 13px;
	}
	.ux-x {
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		cursor: pointer;
		font-size: 14px;
	}
	.ux-scope {
		margin: 0;
		color: var(--rb-text-dim);
		line-height: 1.4;
	}
	.ux-section {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.ux-section h3 {
		margin: 0;
		font-size: 11px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--rb-text-dim);
	}
	.ux-sticks {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.ux-stick {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px 6px;
		border: 1px solid var(--rb-border);
	}
	.ux-stick.picked {
		border-color: var(--rb-accent);
	}
	.ux-pick {
		flex: 1;
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		cursor: pointer;
	}
	.ux-name {
		font-weight: 600;
	}
	.ux-path {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ux-dim {
		color: var(--rb-text-dim);
		margin: 0;
	}
	.ux-field {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.ux-field input {
		padding: 4px 6px;
		background: var(--rb-bg, #0e1218);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font: inherit;
	}
	.ux-actions {
		display: flex;
		gap: 6px;
	}
	.ux-btn {
		padding: 3px 10px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font: inherit;
		cursor: pointer;
	}
	.ux-btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.ux-write {
		border-color: var(--rb-accent);
	}
	.ux-plan {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 2px 10px;
		margin: 0;
	}
	.ux-plan dt {
		color: var(--rb-text-dim);
	}
	.ux-plan dd {
		margin: 0;
		overflow-wrap: anywhere;
	}
	.ux-ok {
		margin: 0;
		color: var(--rb-green, #5bc06a);
	}
	.ux-problem {
		margin: 0;
		color: var(--rb-red, #e0594f);
		overflow-wrap: anywhere;
	}
</style>
