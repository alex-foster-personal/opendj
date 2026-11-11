<script lang="ts">
	/**
	 * #328 USB stick tracker panel (stub).
	 * Present volumes + first-seen modal + folded non-music/forgotten list.
	 * Read-only: never writes to a USB mount.
	 * Future agentic check/fix: (private path)/
	 */
	import {
		applyFirstSeen,
		closeUsbPanel,
		foldedAway,
		presentNonForgotten,
		refreshDeckHealthForVolumes,
		setForgotten,
		usbRowKindLabel,
		usbRowReasonTag,
		usbDiscoveryNotice,
		usbFormatReport,
		usbPollDelayMs,
		usbTracker,
		type UsbDeckHealthState,
		type UsbVolumeKnown
	} from '$lib/rb/usb-tracker.svelte';
	import { canImportUsbVolume, importUsbVolume, USB_IMPORT_TITLE } from '$lib/rb/usb-import';
	import { usbImportView } from '$lib/rb/usb-import-view.svelte';
	import type { Component } from 'svelte';

	let PlanView = $state<Component | null>(null);
	let planFailed = $state(false);

	$effect(() => {
		if (!usbImportView.open || PlanView !== null || planFailed) return;
		void import('./UsbImportPlan.svelte').then(
			(mod) => {
				PlanView = mod.default;
			},
			(exc: unknown) => {
				planFailed = true;
				console.error('[usb] the plan view failed to load', exc);
			}
		);
	});

	let promptName = $state('');
	let draftYours = $state<boolean | null>(null);
	let draftMusic = $state<boolean | null>(null);
	let draftForget = $state(false);

	const open = $derived(usbTracker.panelOpen);
	const formatReport = $derived(
		usbFormatReport(usbTracker.volumes, usbTracker.readNotes, usbTracker.scannedAt !== null)
	);
	const active = $derived(presentNonForgotten());
	// A refused or unanswered scan is not "no stick present": say which.
	const discoveryNotice = $derived(usbDiscoveryNotice(usbTracker));
	const retrySeconds = $derived(usbPollDelayMs(usbTracker.consecutiveFailures) / 1000);
	const folded = $derived(foldedAway());
	const healthVolumeIds = $derived(active.map((v) => v.id));

	$effect(() => {
		if (!open || healthVolumeIds.length === 0) return;
		void refreshDeckHealthForVolumes(healthVolumeIds);
	});

	function deckHealthLabel(state: UsbDeckHealthState | undefined): string {
		if (state === undefined || state.state === 'loading') return 'checking...';
		if (state.state === 'busy') return 'busy (eject or export in progress)';
		if (state.state === 'error') return `unavailable: ${state.message}`;
		const fails = state.report.deck_classes.filter((c) => !c.ok);
		if (fails.length === 0) return 'pass on all deck classes';
		return fails.map((c) => `${c.deck_class}: ${c.failures.join(', ')}`).join('; ');
	}

	const prompt = $derived(
		usbTracker.promptId
			? (usbTracker.volumes.find((v) => v.id === usbTracker.promptId) ?? null)
			: null
	);

	const confirmValid = $derived(draftForget || (draftYours !== null && draftMusic !== null));

	$effect(() => {
		if (prompt) {
			promptName = prompt.name;
			draftYours = null;
			draftMusic = null;
			draftForget = false;
		}
	});

	function close(): void {
		closeUsbPanel();
	}

	function clearDraft(): void {
		draftYours = null;
		draftMusic = null;
		draftForget = false;
	}

	function confirmPrompt(vol: UsbVolumeKnown): void {
		if (!confirmValid) return;
		if (draftForget) {
			applyFirstSeen(vol.id, {
				forgotten: true,
				name: promptName || vol.name
			});
			return;
		}
		applyFirstSeen(vol.id, {
			yours: draftYours === true,
			is_music: draftMusic === true,
			name: promptName || vol.name
		});
	}
</script>

{#if open}
	<div class="usb-backdrop" role="presentation" onclick={close}></div>
	<aside
		class="usb-panel"
		class:planning={usbImportView.open}
		role="dialog"
		aria-label="USB volumes"
		aria-modal="true"
		data-testid="usb-panel"
	>
		<header class="usb-head">
			<strong title="USB stick tracker (#328) - detect only, never writes">USB sticks</strong>
			<button type="button" class="usb-x" onclick={close} title="Close" aria-label="Close">x</button
			>
		</header>

		<section class="usb-found" aria-label="What was found on USB" data-testid="usb-format-report">
			<p class="usb-found-lead" title="How many volumes are mounted right now">
				{formatReport.drives}
			</p>
			<p class="usb-found-lead" title="rekordbox (RBX) only, djay only, or both">
				{formatReport.libraries}
			</p>
			{#each formatReport.sticks as stick (stick.id)}
				<p class="usb-ver" data-testid="usb-format-version">
					<strong>{stick.name}.</strong>
					{stick.version}
				</p>
			{/each}
		</section>

		{#if prompt}
			<section class="usb-prompt" aria-label="First-seen USB">
				<p class="usb-prompt-title">New volume</p>
				<label class="usb-field">
					<span title="Display name for this stick">Name</span>
					<input bind:value={promptName} title="Display name for this stick" />
				</label>

				{#if !draftForget && draftYours === null}
					<p class="usb-q" title="Is this your USB stick?">Yours?</p>
					<div class="usb-row">
						<button type="button" onclick={() => (draftYours = true)}>Yes</button>
						<button type="button" onclick={() => (draftYours = false)}>No / shared</button>
					</div>
				{:else if !draftForget && draftYours !== null}
					<p class="usb-chip" title="Yours answer">
						Yours: {draftYours ? 'yes' : 'no / shared'}
					</p>
				{/if}

				{#if !draftForget && draftMusic === null}
					<p class="usb-q" title="Does this stick hold DJ/music library files?">Music stick?</p>
					<div class="usb-row">
						<button type="button" onclick={() => (draftMusic = true)}>Music</button>
						<button type="button" onclick={() => (draftMusic = false)}>Not music</button>
					</div>
				{:else if !draftForget && draftMusic !== null}
					<p class="usb-chip" title="Music answer">
						Music: {draftMusic ? 'yes' : 'not music'}
					</p>
				{/if}

				{#if !draftForget && draftYours === null && draftMusic === null}
					<button
						type="button"
						class="usb-forget"
						title="Forget this volume (hide from ribbon)"
						onclick={() => (draftForget = true)}
					>
						Forget
					</button>
				{:else if draftForget}
					<p class="usb-chip" title="Forget selected">Forget: yes (hide from ribbon)</p>
				{/if}

				{#if confirmValid}
					<button
						type="button"
						class="usb-confirm"
						title="Confirm first-seen answers"
						onclick={() => confirmPrompt(prompt)}
					>
						Confirm
					</button>
				{/if}
				{#if draftYours !== null || draftMusic !== null || draftForget}
					<button type="button" class="usb-undo" title="Clear draft answers" onclick={clearDraft}>
						undo
					</button>
				{/if}
			</section>
		{/if}

		{#if usbImportView.open}
			{#if PlanView}
				<PlanView />
			{:else if planFailed}
				<p class="usb-empty">
					Analysis plan failed to load. The stick is still queued.
					<button
						type="button"
						data-testid="usb-import-plan-retry"
						onclick={() => (planFailed = false)}>Retry</button
					>
				</p>
			{:else}
				<p class="usb-empty" data-testid="usb-import-plan-loading">Ranking tracks...</p>
			{/if}
		{/if}

		<section class="usb-list" aria-label="Present USB volumes">
			{#if discoveryNotice !== null}
				<p
					class="usb-empty"
					data-testid="usb-discovery-notice"
					title={`${discoveryNotice}. Retrying in ${retrySeconds} s (the wait doubles after each failed check, up to 60 s). ${usbTracker.consecutiveFailures} failed check${usbTracker.consecutiveFailures === 1 ? '' : 's'} in a row.`}
				>
					{discoveryNotice}
				</p>
			{:else if active.length === 0}
				<p class="usb-empty" title="No non-forgotten music USB volumes currently mounted">
					No music USB present
				</p>
			{:else}
				{#each active as vol (vol.id)}
					<div class="usb-item" class:sim={vol.simulated}>
						<div class="usb-item-main">
							<span class="usb-name" title={vol.mount_path ?? vol.id}>{vol.name}</span>
							<span class="usb-kind" title={usbRowKindLabel(vol)}>
								{usbRowKindLabel(vol)}
							</span>
						</div>
						<button
							type="button"
							class="usb-mini"
							data-testid="usb-import-button"
							title={USB_IMPORT_TITLE}
							disabled={!canImportUsbVolume(vol)}
							onclick={() => void importUsbVolume(vol)}
						>
							Open
						</button>
						<button
							type="button"
							class="usb-mini"
							title="Forget this volume"
							onclick={() => setForgotten(vol.id, true)}
						>
							forget
						</button>
					</div>
				{/each}
			{/if}
		</section>

		<details class="usb-fold">
			<summary title="Non-music mounts and forgotten volumes with reason tags">
				Non-music + Forgotten ({folded.length})
			</summary>
			{#each folded as vol (vol.id)}
				{@const reason = usbRowReasonTag(vol)}
				<div class="usb-item dim">
					<div class="usb-item-main">
						<span class="usb-name" title={vol.mount_path ?? vol.id}>{vol.name}</span>
						<span class="usb-kind" title={usbRowKindLabel(vol)}>{usbRowKindLabel(vol)}</span>
						{#if reason}
							<span class="usb-reason" title={reason}>{reason}</span>
						{/if}
					</div>
					{#if vol.forgotten}
						<button
							type="button"
							class="usb-mini"
							title="Unforget this volume"
							onclick={() => setForgotten(vol.id, false)}
						>
							unforget
						</button>
					{/if}
				</div>
			{/each}
			{#if folded.length === 0}
				<p class="usb-empty">None</p>
			{/if}
		</details>

		<section class="usb-soon" aria-label="Deck export health" data-testid="usb-deck-health">
			<p class="usb-soon-title">Deck export health (rules v0)</p>
			<p class="usb-hint" title="Read-only scan; refused while eject or usb-export holds the stick">
				Read-only per-stick check (FAT/exFAT, export.pdb / exportLibrary.db, path limits). No
				writes.
			</p>
			{#each active as vol (vol.id)}
				{@const deckHealth = usbTracker.deckHealth[vol.id]}
				<div class="usb-health-row" data-testid="usb-deck-health-row">
					<strong>{vol.name}</strong>
					<span class="usb-health-detail" title={deckHealthLabel(deckHealth)}>
						{deckHealthLabel(deckHealth)}
					</span>
					{#if deckHealth?.state === 'ok'}
						<ul class="usb-health-classes">
							{#each deckHealth.report.deck_classes as deck (deck.deck_class)}
								<li class:fail={!deck.ok}>
									{deck.label}: {deck.ok ? 'pass' : deck.failures.join(', ')}
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{:else}
				<p class="usb-empty">No music USB present</p>
			{/each}
			<p class="usb-hint">Read/write speed benchmarking is still not measured.</p>
		</section>

		<footer class="usb-foot">
			<span
				class="usb-hint"
				title="Detect + open. Analysis runs in the background and never writes to the stick."
			>
				Detect + open. Analysis runs in the background and never writes to the stick.
			</span>
		</footer>
	</aside>
{/if}

<style>
	.usb-backdrop {
		position: absolute;
		inset: 0;
		z-index: 40;
		background: rgba(0, 0, 0, 0.35);
	}
	.usb-panel {
		position: absolute;
		right: 8px;
		bottom: 26px;
		z-index: 41;
		width: min(320px, calc(100% - 16px));
		max-height: min(560px, 78%);
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 10px;
		background: var(--rb-panel-raised, #1a1f28);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 12px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
		overflow: auto;
	}
	.usb-panel.planning {
		top: 8px;
		width: calc(100% - 16px);
		max-height: calc(100% - 34px);
	}
	.usb-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.usb-found,
	.usb-soon {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin: 0;
		padding: 6px;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel);
	}
	.usb-soon {
		border-style: dashed;
	}
	.usb-found-lead,
	.usb-soon-title,
	.usb-ver,
	.usb-soon p,
	.usb-soon li {
		margin: 0;
	}
	.usb-found-lead {
		font-weight: 700;
	}
	.usb-ver {
		color: var(--rb-text-dim);
		font-size: 10px;
	}
	.usb-soon-title {
		font-weight: 700;
		color: var(--rb-orange, #e8952a);
	}
	.usb-health-row {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 4px 0;
		border-top: 1px solid var(--rb-border);
	}
	.usb-health-detail {
		font-size: 10px;
		color: var(--rb-text-dim);
	}
	.usb-health-classes {
		margin: 0;
		padding-left: 16px;
		font-size: 10px;
		color: var(--rb-text-dim);
	}
	.usb-health-classes li.fail {
		color: var(--rb-orange, #e8952a);
	}
	.usb-x {
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		cursor: pointer;
		font-size: 14px;
	}
	.usb-prompt {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 8px;
		border: 1px solid var(--rb-orange, #e8952a);
		background: color-mix(in srgb, var(--rb-orange, #e8952a) 10%, transparent);
	}
	.usb-prompt-title {
		margin: 0;
		font-weight: 700;
		color: var(--rb-orange, #e8952a);
	}
	.usb-field {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.usb-field input {
		padding: 4px 6px;
		background: var(--rb-bg, #0e1218);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
	}
	.usb-q {
		margin: 4px 0 0;
		font-weight: 600;
	}
	.usb-chip {
		margin: 0;
		color: var(--rb-text-dim);
		font-size: 11px;
	}
	.usb-row {
		display: flex;
		gap: 6px;
	}
	.usb-row button,
	.usb-forget,
	.usb-confirm,
	.usb-mini,
	.usb-undo {
		padding: 4px 8px;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel);
		color: var(--rb-text);
		cursor: pointer;
		font: inherit;
	}
	.usb-forget {
		align-self: flex-start;
		color: var(--rb-text-dim);
	}
	.usb-confirm {
		align-self: stretch;
		font-weight: 700;
		border-color: var(--rb-orange, #e8952a);
		color: var(--rb-orange, #e8952a);
	}
	.usb-undo {
		align-self: flex-start;
		border: none;
		background: transparent;
		color: var(--rb-text-dim);
		font-size: 11px;
		text-decoration: underline;
		padding: 0 2px;
	}
	.usb-list {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.usb-item {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 4px 6px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
	}
	.usb-item.sim {
		border-color: color-mix(in srgb, var(--rb-orange, #e8952a) 50%, var(--rb-border));
	}
	.usb-item.dim {
		opacity: 0.7;
	}
	.usb-item-main {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.usb-name {
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.usb-kind {
		font-size: 10px;
		color: var(--rb-text-dim);
		text-transform: none;
	}
	.usb-reason {
		font-size: 10px;
		color: var(--rb-orange, #e8952a);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.usb-empty {
		margin: 0;
		color: var(--rb-text-dim);
	}
	.usb-fold {
		border-top: 1px solid var(--rb-border);
		padding-top: 6px;
	}
	.usb-fold summary {
		cursor: pointer;
		color: var(--rb-text-dim);
		margin-bottom: 4px;
	}
	.usb-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-top: 4px;
	}
	.usb-hint {
		font-size: 10px;
		color: var(--rb-text-dim);
	}
</style>
