<script lang="ts">
	/**
	 * #328 USB stick tracker panel (stub).
	 * Present volumes + first-seen modal + folded non-music/forgotten list.
	 * Read-only: never writes to a USB mount.
	 */
	import {
		applyFirstSeen,
		closeUsbPanel,
		foldedAway,
		presentNonForgotten,
		setForgotten,
		usbRowKindLabel,
		usbRowReasonTag,
		usbDiscoveryNotice,
		usbPollDelayMs,
		usbTracker,
		type UsbVolumeKnown
	} from '$lib/rb/usb-tracker.svelte';
	import { canImportUsbVolume, importUsbVolume } from '$lib/rb/usb-import';

	const IMPORT_TITLE =
		'Import tracks from this stick. Files stay on the stick. Tags only - no BPM, key, or beatgrid.';

	let foldedOpen = $state(false);
	let promptName = $state('');
	let draftYours = $state<boolean | null>(null);
	let draftMusic = $state<boolean | null>(null);
	let draftForget = $state(false);

	const open = $derived(usbTracker.panelOpen);
	const active = $derived(presentNonForgotten());
	// A refused or unanswered scan is not "no stick present": say which.
	const discoveryNotice = $derived(usbDiscoveryNotice(usbTracker));
	const retrySeconds = $derived(usbPollDelayMs(usbTracker.consecutiveFailures) / 1000);
	const folded = $derived(foldedAway());
	const prompt = $derived(
		usbTracker.promptId
			? (usbTracker.volumes.find((v) => v.id === usbTracker.promptId) ?? null)
			: null
	);

	const confirmValid = $derived(
		draftForget || (draftYours !== null && draftMusic !== null)
	);

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
	<aside class="usb-panel" role="dialog" aria-label="USB volumes" aria-modal="true" data-testid="usb-panel">
		<header class="usb-head">
			<strong title="USB stick tracker (#328) - detect only, never writes">USB sticks</strong>
			<button type="button" class="usb-x" onclick={close} title="Close" aria-label="Close">x</button>
		</header>

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
							title={IMPORT_TITLE}
							disabled={!canImportUsbVolume(vol)}
							onclick={() => void importUsbVolume(vol)}
						>
							import
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

		<details class="usb-fold" bind:open={foldedOpen}>
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
							restore
						</button>
					{/if}
				</div>
			{/each}
			{#if folded.length === 0}
				<p class="usb-empty">None</p>
			{/if}
		</details>

		<footer class="usb-foot">
			<span class="usb-hint" title="Detect + import; never writes to the stick">
				Detect + import; never writes to the stick.
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
		max-height: min(420px, 70%);
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
	.usb-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
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
