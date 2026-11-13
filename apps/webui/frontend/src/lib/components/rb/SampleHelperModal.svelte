<script lang="ts">
	/**
	 * Paste a YouTube short or watch URL, rip audio through the engine, stage to
	 * the ingest inbox, and preview the staged file before closing.
	 */
	import {
		downloadSampleFromUrl,
		samplePreviewSrc,
		type SampleDownloadOut
	} from '$lib/rb/api-sample-helper';
	import { startIngestRefresh } from '$lib/rb/api-ingest';
	import { refreshIngestPending } from '$lib/rb/ingest-pending.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import { focusTrap } from '$lib/keyboard/focus-trap';

	let { open = $bindable(false) } = $props<{
		open?: boolean;
	}>();

	let url = $state('');
	let batch = $state('');
	let busy = $state(false);
	let result = $state<SampleDownloadOut | null>(null);

	const previewSrc = $derived(samplePreviewSrc(result?.preview_url ?? null));

	function _defaultBatch(): string {
		const d = new Date();
		const p = (n: number) => String(n).padStart(2, '0');
		return `yt-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
	}

	let wasOpen = false;
	$effect(() => {
		if (open && !wasOpen) {
			url = '';
			batch = _defaultBatch();
			result = null;
			busy = false;
		}
		wasOpen = open;
	});

	function close(): void {
		open = false;
		result = null;
		url = '';
	}

	function onKeydown(e: KeyboardEvent): void {
		if (open && e.key === 'Escape') close();
	}

	/** Escape closes from inside; Tab reaches the focus trap; every other key stops here, off the page shortcuts. */
	function onDialogKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape') {
			e.preventDefault();
			close();
		}
		if (e.key !== 'Tab') e.stopPropagation();
	}

	async function download(): Promise<void> {
		const trimmed = url.trim();
		if (!trimmed) {
			pushToast('Paste a YouTube URL first', 'error');
			return;
		}
		busy = true;
		try {
			result = await downloadSampleFromUrl(trimmed, batch);
			const staged = result.results.filter((r) => r.staged_path !== null).length;
			if (staged > 0) {
				await startIngestRefresh(result.dest_dir);
				await refreshIngestPending();
			}
			pushToast(
				staged > 0
					? `staged sample ${result.video_id} to ingest`
					: `held sample ${result.video_id} (duplicate policy)`,
				staged > 0 ? 'info' : 'warn'
			);
		} catch (err) {
			pushToast(`sample download failed: ${err instanceof Error ? err.message : err}`, 'error');
		} finally {
			busy = false;
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

{#if open}
	<div class="scrim" role="presentation" onclick={close} onkeydown={onKeydown}>
		<div
			class="modal"
			role="dialog"
			aria-modal="true"
			aria-label="Sample from YouTube"
			data-testid="sample-helper-modal"
			tabindex="-1"
			use:focusTrap
			onclick={(e) => e.stopPropagation()}
			onkeydown={onDialogKeydown}
		>
			<div class="m-title">Paste YouTube link</div>
			<label class="m-field">
				URL
				<input
					type="url"
					bind:value={url}
					placeholder="https://www.youtube.com/shorts/..."
					spellcheck="false"
					disabled={busy}
					data-testid="sample-helper-url"
				/>
			</label>
			<label class="m-field">
				batch
				<input type="text" bind:value={batch} spellcheck="false" disabled={busy} />
			</label>
			{#if result?.preview_url}
				<audio
					class="m-preview"
					controls
					src={previewSrc ?? undefined}
					data-testid="sample-helper-preview"
				></audio>
				<div class="m-note">Staged under {result.dest_dir}</div>
			{/if}
			<div class="m-actions">
				<button class="m-btn" type="button" onclick={close} disabled={busy}>Close</button>
				<button
					class="m-btn primary"
					type="button"
					data-testid="sample-helper-download"
					disabled={busy}
					onclick={() => void download()}
				>
					{busy ? 'Downloading...' : 'Download & stage'}
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.scrim {
		position: fixed;
		inset: 0;
		z-index: 12000;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgb(0 0 0 / 55%);
	}
	.modal {
		width: min(28rem, 92vw);
		padding: 1rem 1.1rem;
		border-radius: 8px;
		background: var(--rb-panel, #1a1a1e);
		color: var(--rb-fg, #eee);
		box-shadow: 0 12px 40px rgb(0 0 0 / 45%);
	}
	.m-title {
		font-size: 1.05rem;
		font-weight: 600;
		margin-bottom: 0.75rem;
	}
	.m-field {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		margin-bottom: 0.65rem;
		font-size: 0.85rem;
	}
	.m-field input {
		padding: 0.4rem 0.5rem;
		border: 1px solid rgb(255 255 255 / 18%);
		border-radius: 4px;
		background: rgb(0 0 0 / 25%);
		color: inherit;
	}
	.m-preview {
		width: 100%;
		margin: 0.5rem 0;
	}
	.m-note {
		font-size: 0.8rem;
		opacity: 0.85;
		margin-bottom: 0.5rem;
	}
	.m-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
		margin-top: 0.75rem;
	}
	.m-btn {
		padding: 0.35rem 0.75rem;
		border-radius: 4px;
		border: 1px solid rgb(255 255 255 / 20%);
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	.m-btn.primary {
		background: var(--rb-accent, #3d7eff);
		border-color: transparent;
		color: var(--rb-on-accent, #fff);
	}
	.m-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
