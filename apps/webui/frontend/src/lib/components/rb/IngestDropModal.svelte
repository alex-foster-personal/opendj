<script lang="ts">
	/**
	 * Drag-in ingestion modal (performance page).
	 *
	 * External file drags (dataTransfer includes Files) show a drop overlay;
	 * dropping audio files opens this modal: batch name, the persisted step
	 * checkbox list from /ingest/config, then Stage & run - uploads the
	 * files (server-side duration+chromaprint duplicate check; exact dups
	 * are skipped and reported, linking the existing track's metadata), and
	 * kicks the batch-scoped refresh (analysis now; stems/vocals after the
	 * Rekordbox import, which stays a manual step by design).
	 */
	import {
		decideIngestUpload,
		getIngestConfig,
		putIngestConfig,
		startIngestRefresh,
		uploadIngestFiles,
		type IngestConfig,
		type UploadFileResult,
		type UploadOut
	} from '$lib/rb/api-ingest';
	import { collectDroppedAudioFiles } from '$lib/rb/ingest-drop-files';
	import { refreshIngestPending } from '$lib/rb/ingest-pending.svelte';
	import { pushToast } from '$lib/stores.svelte';

	let dragDepth = $state(0);
	let open = $state(false);
	let files = $state<File[]>([]);
	let batch = $state('');
	let config = $state<IngestConfig | null>(null);
	let busy = $state(false);
	let result = $state<UploadOut | null>(null);

	const hasPossibleDups = $derived(
		result?.results.some((r) => r.verdict === 'possible_duplicate') ?? false
	);

	function _defaultBatch(): string {
		const d = new Date();
		const p = (n: number) => String(n).padStart(2, '0');
		return `drop-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
	}

	function _isFileDrag(e: DragEvent): boolean {
		return e.dataTransfer?.types.includes('Files') === true;
	}

	function _isOverPlaylistTree(target: EventTarget | null): boolean {
		return target instanceof Element && target.closest('[data-testid="playlist-tree-panel"]') !== null;
	}

	function onDragEnter(e: DragEvent): void {
		if (!_isFileDrag(e) || _isOverPlaylistTree(e.target)) return;
		e.preventDefault();
		dragDepth += 1;
	}

	function onDragOver(e: DragEvent): void {
		if (_isFileDrag(e)) e.preventDefault();
	}

	function onDragLeave(e: DragEvent): void {
		if (!_isFileDrag(e)) return;
		dragDepth = Math.max(0, dragDepth - 1);
	}

	async function onDrop(e: DragEvent): Promise<void> {
		if (!_isFileDrag(e) || _isOverPlaylistTree(e.target)) return;
		e.preventDefault();
		dragDepth = 0;
		const dropped = await collectDroppedAudioFiles(e.dataTransfer!);
		if (dropped.length === 0) {
			pushToast('No audio files in that drop', 'error');
			return;
		}
		files = dropped;
		batch = _defaultBatch();
		result = null;
		open = true;
		try {
			config = await getIngestConfig();
		} catch (err) {
			pushToast(`ingest config unavailable: ${err instanceof Error ? err.message : err}`, 'error');
			config = null;
		}
	}

	async function toggleStep(id: string, enabled: boolean): Promise<void> {
		try {
			config = await putIngestConfig({ [id]: enabled });
		} catch (err) {
			pushToast(`config update failed: ${err instanceof Error ? err.message : err}`, 'error');
		}
	}

	async function _maybeStartRefresh(): Promise<void> {
		if (!result) return;
		const remaining = result.results.some((r) => r.verdict === 'possible_duplicate');
		if (remaining) return;
		const staged = result.results.filter((r) => r.verdict === 'new').length;
		if (staged > 0) {
			await startIngestRefresh(result.dest_dir);
		}
		await refreshIngestPending();
	}

	async function stageAndRun(): Promise<void> {
		busy = true;
		try {
			result = await uploadIngestFiles(files, batch);
			const staged = result.results.filter((r) => r.verdict === 'new').length;
			const skipped = result.results.filter((r) => r.verdict === 'skipped_duplicate').length;
			const held = result.results.filter((r) => r.verdict === 'possible_duplicate').length;
			pushToast(`staged ${staged}, held ${held}, skipped ${skipped} duplicate(s)`, 'info');
			await _maybeStartRefresh();
		} catch (err) {
			pushToast(`ingest failed: ${err instanceof Error ? err.message : err}`, 'error');
		} finally {
			busy = false;
		}
	}

	async function acceptDup(r: UploadFileResult): Promise<void> {
		if (!result) return;
		busy = true;
		try {
			const decided = await decideIngestUpload({
				batch: result.batch,
				filename: r.filename,
				action: 'accept'
			});
			result = {
				...result,
				results: result.results.map((row) => (row.filename === r.filename ? decided : row))
			};
			await _maybeStartRefresh();
		} catch (err) {
			pushToast(`accept failed: ${err instanceof Error ? err.message : err}`, 'error');
		} finally {
			busy = false;
		}
	}

	async function rejectDup(r: UploadFileResult): Promise<void> {
		if (!result) return;
		busy = true;
		try {
			const decided = await decideIngestUpload({
				batch: result.batch,
				filename: r.filename,
				action: 'reject'
			});
			result = {
				...result,
				results: result.results.map((row) => (row.filename === r.filename ? decided : row))
			};
			await _maybeStartRefresh();
		} catch (err) {
			pushToast(`reject failed: ${err instanceof Error ? err.message : err}`, 'error');
		} finally {
			busy = false;
		}
	}

	/**
	 * A floating promise from an event attribute drops its rejection on the
	 * floor: the drop handler failed silently for a whole release because
	 * nothing was listening. Report it where the user already looks.
	 */
	async function _onDropReported(e: DragEvent): Promise<void> {
		try {
			await onDrop(e);
		} catch (err) {
			pushToast(`drop failed: ${err instanceof Error ? err.message : err}`, 'error');
			throw err;
		}
	}

	function close(): void {
		open = false;
		files = [];
		result = null;
	}

	function onKeydown(e: KeyboardEvent): void {
		if (open && e.key === 'Escape') close();
	}
</script>

<svelte:window
	ondragenter={onDragEnter}
	ondragover={onDragOver}
	ondragleave={onDragLeave}
	ondrop={(e) => void _onDropReported(e)}
	onkeydown={onKeydown}
/>

{#if dragDepth > 0 && !open}
	<div class="drop-overlay" data-testid="ingest-drop-overlay">
		<div class="drop-box">Drop audio files to ingest</div>
	</div>
{/if}

{#if open}
	<!-- scrim click closes; Escape handled on window; inner stops propagation -->
	<div class="scrim" role="presentation" onclick={close} onkeydown={onKeydown}>
		<div
			class="modal"
			role="dialog"
			aria-label="Ingest new tracks"
			data-testid="ingest-modal"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
			onkeydown={(e) => e.stopPropagation()}
		>
			<div class="m-title">Ingest {files.length} file{files.length === 1 ? '' : 's'}</div>

			<label class="m-batch">
				batch
				<input type="text" bind:value={batch} spellcheck="false" />
			</label>

			<div class="m-steps">
				{#if config === null}
					<div class="m-err">config unavailable - daemon down?</div>
				{:else}
					{#each config.steps as step (step.id)}
						<label class="m-step" title={step.hint}>
							<input
								type="checkbox"
								checked={step.enabled}
								disabled={busy}
								onchange={(e) => toggleStep(step.id, e.currentTarget.checked)}
							/>
							{step.label}
							{#if step.requires_rb_row}
								<span class="m-note">after RB import</span>
							{/if}
						</label>
					{/each}
				{/if}
			</div>

			{#if result === null}
				<ul class="m-files">
					{#each files as f (f.name)}
						<li>{f.name}</li>
					{/each}
				</ul>
			{:else}
				<ul class="m-files">
					{#each result.results as r (r.filename)}
						<li class:dup={r.verdict === 'skipped_duplicate' || r.verdict === 'possible_duplicate'}>
							{r.filename}
							{#if r.verdict === 'skipped_duplicate' && r.duplicate_of !== null}
								- duplicate of {r.duplicate_of.title ?? r.duplicate_of.stable_id.slice(0, 8)}
								({r.duplicate_of.method}{r.duplicate_of.score !== null ? ` ${r.duplicate_of.score}` : ''})
								- kept the existing track's metadata
							{:else if r.verdict === 'possible_duplicate' && r.duplicate_of !== null}
								- possible dup: {r.duplicate_of.title ?? r.duplicate_of.stable_id.slice(0, 8)}
								({r.duplicate_of.method}{r.duplicate_of.score !== null ? ` ${r.duplicate_of.score}` : ''})
								<span class="m-dup-actions">
									<button
										type="button"
										class="m-btn small"
										data-testid="ingest-dup-accept"
										disabled={busy}
										onclick={() => acceptDup(r)}
									>
										Accept as new
									</button>
									<button
										type="button"
										class="m-btn small"
										data-testid="ingest-dup-reject"
										disabled={busy}
										onclick={() => rejectDup(r)}
									>
										Skip
									</button>
								</span>
							{/if}
						</li>
					{/each}
				</ul>
				<div class="m-next">
					Staged to {result.dest_dir}.
					{#if hasPossibleDups}
						Resolve possible duplicates above before analysis runs.
					{:else}
						Analysis is running (watch the TopBar refresh arrow). Import the folder into
						Rekordbox, then hit refresh again for stems/vocals.
					{/if}
				</div>
			{/if}

			<div class="m-actions">
				<button class="m-btn" onclick={close} disabled={busy}>Close</button>
				{#if result === null}
					<button
						class="m-btn primary"
						data-testid="ingest-run"
						onclick={stageAndRun}
						disabled={busy || batch.length === 0}
					>
						{busy ? 'Staging…' : 'Stage & run'}
					</button>
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.drop-overlay {
		position: fixed;
		inset: 0;
		z-index: 70;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(10, 12, 15, 0.65);
		pointer-events: none;
	}
	.drop-box {
		padding: 18px 28px;
		border: 2px dashed #4cc9f0;
		border-radius: 8px;
		color: #d7dde3;
		font-size: 14px;
		background: #14171b;
	}
	.scrim {
		position: fixed;
		inset: 0;
		z-index: 80;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.55);
	}
	.modal {
		width: 440px;
		max-height: 70vh;
		overflow-y: auto;
		padding: 14px 16px;
		background: #14171b;
		border: 1px solid #2a2f36;
		border-radius: 6px;
		color: #c8cfd6;
		font-size: 12px;
	}
	.m-title {
		font-family: var(--rb-font-brand);
		font-size: 13px;
		font-weight: 600;
		color: #e8edf2;
		margin-bottom: 10px;
	}
	.m-batch {
		display: flex;
		gap: 8px;
		align-items: center;
		margin-bottom: 10px;
	}
	.m-batch input {
		flex: 1;
		padding: 4px 6px;
		background: #0d0f12;
		border: 1px solid #2a2f36;
		border-radius: 3px;
		color: #e8edf2;
		font-size: 12px;
	}
	.m-steps {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 10px;
	}
	.m-step {
		display: flex;
		gap: 6px;
		align-items: center;
	}
	.m-note {
		color: #8a939d;
		font-size: 10px;
	}
	.m-err {
		color: #e5484d;
	}
	.m-files {
		margin: 0 0 10px;
		padding-left: 18px;
		max-height: 180px;
		overflow-y: auto;
	}
	.m-files li.dup {
		color: #e8973e;
	}
	.m-dup-actions {
		display: inline-flex;
		gap: 4px;
		margin-left: 4px;
	}
	.m-next {
		margin-bottom: 10px;
		color: #9fd8a8;
	}
	.m-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}
	.m-btn {
		padding: 5px 12px;
		background: #242a31;
		border: 1px solid #2a2f36;
		border-radius: 3px;
		color: #d7dde3;
		cursor: pointer;
		font-size: 12px;
	}
	.m-btn.small {
		padding: 2px 6px;
		font-size: 10px;
	}
	.m-btn.primary {
		background: #1d4ed8;
		border-color: #1d4ed8;
		color: #fff;
	}
	.m-btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
</style>
