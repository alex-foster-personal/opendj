<script lang="ts">
	// Work editor, Link a version, merge confirm and Copy / Unstack. One host, so
	// the table adds a single mount and every modal shares the BrowserConfirmDialog
	// look. Enter takes the default action and Esc cancels (PRD s5.6).
	import { addPlaylistItems } from '$lib/rb/playlist-write';
	import type { ClassicalValues, WorkRole } from '$lib/rb/library-works';
	import { primaryWorkId } from '$lib/rb/library-works';
	import {
		createWork,
		fetchCandidates,
		fetchClassical,
		fetchFileTags,
		fetchWorks,
		linkTracks,
		mergeWorks,
		reportWorksFailure,
		saveClassical,
		setMemberRole,
		unlinkMember,
		updateWork,
		WorksError,
		worksView,
		type WorkRow
	} from '$lib/rb/works-overlay.svelte';

	const ROLES: WorkRole[] = ['original', 'recording', 'remix', 'cover', 'edit', 'live', 'mashup'];
	const WORK_TEXT = ['composer', 'catalogue', 'movement'] as const;
	const RECORDING_TEXT = ['soloist', 'ensemble', 'conductor', 'label'] as const;

	const modal = $derived(worksView.modal);
	const close = (): void => {
		worksView.modal = null;
	};

	// ----- work editor ---------------------------------------------------------
	let works = $state<WorkRow[]>([]);
	let workQuery = $state('');
	let existing = $state<WorkRow | null>(null);
	let originalTitle = $state('');
	let role = $state<WorkRole>('remix');
	let kind = $state<'classical' | 'song'>('classical');
	let workFields = $state<Record<(typeof WORK_TEXT)[number], string>>({
		composer: '',
		catalogue: '',
		movement: ''
	});
	let aliases = $state('');
	let recording = $state<Record<(typeof RECORDING_TEXT)[number], string>>({
		soloist: '',
		ensemble: '',
		conductor: '',
		label: ''
	});
	let year = $state('');
	let editorError = $state<string | null>(null);
	let saving = $state(false);

	const firstId = $derived(modal?.kind === 'editor' ? modal.stableIds[0] : null);
	const affected = $derived(
		existing === null ? 0 : (worksView.overlay.works[existing.id]?.member_count ?? 0)
	);
	const renaming = $derived(existing !== null && workQuery.trim() !== originalTitle);
	const matches = $derived(
		workQuery.trim() === ''
			? []
			: works
					.filter((w) => w.title.toLocaleLowerCase().includes(workQuery.trim().toLocaleLowerCase()))
					.slice(0, 8)
	);

	function pick(work: WorkRow | null): void {
		existing = work;
		originalTitle = work?.title ?? '';
		workQuery = work?.title ?? workQuery;
		kind = work?.kind ?? kind;
		workFields = {
			composer: work?.composer ?? '',
			catalogue: work?.catalogue ?? '',
			movement: work?.movement ?? ''
		};
		aliases = work === null ? '' : (JSON.parse(work.aliases) as string[]).join(', ');
	}

	$effect(() => {
		if (firstId === null) return;
		const id = firstId;
		editorError = null;
		void (async () => {
			try {
				const [list, classical] = await Promise.all([fetchWorks(), fetchClassical(id)]);
				works = list.rows;
				const current = primaryWorkId(worksView.overlay, id);
				const link = worksView.overlay.members[id]?.find((m) => m.work_id === current);
				role = link?.role ?? 'remix';
				workQuery = modal?.kind === 'editor' ? modal.title : '';
				pick(list.rows.find((w) => w.id === current) ?? null);
				const values = classical.values;
				recording = {
					soloist: values.soloist ?? '',
					ensemble: values.ensemble ?? '',
					conductor: values.conductor ?? '',
					label: values.label ?? ''
				};
				year = values.recording_year === undefined ? '' : String(values.recording_year);
			} catch (exc) {
				editorError = exc instanceof Error ? exc.message : String(exc);
			}
		})();
	});

	async function readFileTags(): Promise<void> {
		if (firstId === null) return;
		try {
			const { proposed } = await fetchFileTags(firstId);
			const text = (key: string): string | null =>
				typeof proposed[key] === 'string' ? (proposed[key] as string) : null;
			workQuery = existing === null ? (text('work') ?? workQuery) : workQuery;
			workFields = {
				composer: text('composer') ?? workFields.composer,
				catalogue: text('catalogue') ?? workFields.catalogue,
				movement: text('movement') ?? workFields.movement
			};
			recording = {
				...recording,
				conductor: text('conductor') ?? recording.conductor,
				label: text('label') ?? recording.label
			};
			if (typeof proposed.recording_year === 'number') year = String(proposed.recording_year);
			editorError = Object.keys(proposed).length === 0 ? 'This file has no such tags.' : null;
		} catch (exc) {
			editorError = exc instanceof Error ? exc.message : String(exc);
		}
	}

	function workPayload(): Record<string, unknown> {
		return {
			title: workQuery.trim(),
			kind,
			composer: workFields.composer.trim() || null,
			catalogue: workFields.catalogue.trim() || null,
			movement: workFields.movement.trim() || null,
			aliases: aliases
				.split(',')
				.map((alias) => alias.trim())
				.filter(Boolean)
		};
	}

	function recordingPayload(): ClassicalValues {
		const parsedYear = year.trim() === '' ? null : Number(year);
		const values: Record<string, unknown> = { recording_year: parsedYear };
		for (const name of RECORDING_TEXT) values[name] = recording[name];
		return values as ClassicalValues;
	}

	async function saveEditor(): Promise<void> {
		if (modal?.kind !== 'editor' || workQuery.trim() === '' || saving) return;
		saving = true;
		editorError = null;
		try {
			const id = existing?.id ?? String((await createWork(workPayload())).row.id);
			if (existing !== null) await updateWork(id, workPayload());
			await linkTracks(modal.stableIds, { workId: id, role });
			for (const stableId of modal.stableIds) {
				await setMemberRole(id, stableId, role);
				await saveClassical(stableId, recordingPayload());
			}
			close();
		} catch (exc) {
			if (exc instanceof WorksError && typeof exc.details.existing_work_id === 'string') {
				const intoId = exc.details.existing_work_id;
				if (existing !== null) {
					worksView.modal = { kind: 'merge', sourceId: existing.id, intoId, onmerged: close };
					return;
				}
				editorError = `${exc.message}. Pick it from the list above.`;
			} else {
				editorError = exc instanceof Error ? exc.message : String(exc);
			}
		} finally {
			saving = false;
		}
	}

	// ----- link a version ------------------------------------------------------
	let linkQuery = $state('');
	let candidates = $state<{ member_id: string; title: string; artist: string }[]>([]);

	$effect(() => {
		if (modal?.kind !== 'link') return;
		const id = modal.stableId;
		const query = linkQuery;
		fetchCandidates(id, query)
			.then((found) => {
				candidates = found.rows;
			})
			.catch((exc: unknown) => reportWorksFailure('search failed', exc));
	});

	/** Stack two tracks, asking before two works merge (E17). */
	export async function stackTracks(ids: string[], title: string): Promise<void> {
		try {
			await linkTracks(ids, { title });
			close();
		} catch (exc) {
			const clash = exc instanceof WorksError ? exc.details.work_ids : undefined;
			if (Array.isArray(clash) && clash.length === 2) {
				const [sourceId, intoId] = clash as string[];
				worksView.modal = {
					kind: 'merge',
					sourceId,
					intoId,
					onmerged: () => void stackTracks(ids, title)
				};
				return;
			}
			reportWorksFailure('link failed', exc);
		}
	}

	// ----- merge ---------------------------------------------------------------
	const mergeNames = $derived.by(() => {
		if (modal?.kind !== 'merge') return null;
		const overlay = worksView.overlay.works;
		return {
			source: overlay[modal.sourceId],
			into: overlay[modal.intoId]
		};
	});

	async function confirmMerge(): Promise<void> {
		if (modal?.kind !== 'merge') return;
		const { sourceId, intoId, onmerged } = modal;
		try {
			await mergeWorks(sourceId, intoId);
			worksView.modal = null;
			onmerged();
		} catch (exc) {
			reportWorksFailure('merge failed', exc);
		}
	}

	// ----- copy / unstack ------------------------------------------------------
	async function copyOut(): Promise<void> {
		if (modal?.kind !== 'copy-unstack') return;
		await addPlaylistItems(modal.playlistId, [modal.member.member_id], modal.position).catch(
			(exc: unknown) => reportWorksFailure('copy failed', exc)
		);
		close();
	}

	async function unstack(): Promise<void> {
		if (modal?.kind !== 'copy-unstack') return;
		await unlinkMember(modal.workId, modal.member.member_id).catch((exc: unknown) =>
			reportWorksFailure('unstack failed', exc)
		);
		close();
	}

	function onKeydown(event: KeyboardEvent): void {
		if (modal === null) return;
		if (event.key === 'Escape') {
			event.preventDefault();
			close();
		} else if (event.key === 'Enter' && modal.kind === 'copy-unstack') {
			event.preventDefault();
			void copyOut();
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

{#if modal?.kind === 'editor'}
	<div class="wm" role="dialog" aria-modal="true" aria-label="Work" data-testid="work-editor">
		<h3>Work</h3>
		<label class="wm-field">
			Work
			<input bind:value={workQuery} data-testid="work-editor-title" />
		</label>
		{#if existing === null && matches.length > 0}
			<div class="wm-list">
				{#each matches as work (work.id)}
					<button type="button" onclick={() => pick(work)}>{work.title}</button>
				{/each}
			</div>
		{/if}
		{#if renaming}
			<p class="wm-note" data-testid="work-editor-rename-note">
				Renaming affects {affected} track{affected === 1 ? '' : 's'}.
			</p>
		{/if}
		<div class="wm-grid">
			<label class="wm-field">
				Type
				<select bind:value={kind}>
					<option value="classical">Classical</option>
					<option value="song">Song</option>
				</select>
			</label>
			<label class="wm-field">
				Role
				<select bind:value={role} data-testid="work-editor-role">
					{#each ROLES as option (option)}<option value={option}>{option}</option>{/each}
				</select>
			</label>
			{#each WORK_TEXT as name (name)}
				<label class="wm-field">
					{name}
					<input bind:value={workFields[name]} data-testid={`work-editor-${name}`} />
				</label>
			{/each}
			<label class="wm-field">
				aliases
				<input bind:value={aliases} />
			</label>
			{#each RECORDING_TEXT as name (name)}
				<label class="wm-field">
					{name}
					<input bind:value={recording[name]} data-testid={`work-editor-${name}`} />
				</label>
			{/each}
			<label class="wm-field">
				year
				<input bind:value={year} inputmode="numeric" />
			</label>
		</div>
		{#if editorError !== null}<p class="wm-error" data-testid="work-editor-error">{editorError}</p>{/if}
		<div class="wm-actions">
			<button type="button" onclick={() => void readFileTags()}>Read from file tags</button>
			<button
				type="button"
				class="wm-primary"
				data-testid="work-editor-save"
				disabled={workQuery.trim() === '' || saving}
				onclick={() => void saveEditor()}
			>
				Save
			</button>
			<button type="button" onclick={close}>Cancel</button>
		</div>
	</div>
{:else if modal?.kind === 'link'}
	<div class="wm" role="dialog" aria-modal="true" aria-label="Link a version" data-testid="link-version">
		<h3>Link a version</h3>
		<!-- svelte-ignore a11y_autofocus -->
		<input bind:value={linkQuery} placeholder="Search the library" autofocus />
		<div class="wm-list">
			{#each candidates as candidate (candidate.member_id)}
				<button
					type="button"
					onclick={() =>
						modal.kind === 'link' &&
						void stackTracks([modal.stableId, candidate.member_id], candidate.title)}
				>
					{candidate.title} <span class="wm-dim">{candidate.artist}</span>
				</button>
			{/each}
		</div>
		<div class="wm-actions"><button type="button" onclick={close}>Cancel</button></div>
	</div>
{:else if modal?.kind === 'merge' && mergeNames !== null}
	<div class="wm" role="dialog" aria-modal="true" aria-label="Merge works" data-testid="merge-works">
		<h3>Merge works?</h3>
		<p>
			Merge "{mergeNames.source?.title}" ({mergeNames.source?.member_count ?? 0}) into
			"{mergeNames.into?.title}" ({mergeNames.into?.member_count ?? 0}). Tracks stay as they are.
		</p>
		<div class="wm-actions">
			<button type="button" class="wm-primary" onclick={() => void confirmMerge()}>Merge</button>
			<button type="button" onclick={close}>Cancel</button>
		</div>
	</div>
{:else if modal?.kind === 'copy-unstack'}
	<div class="wm" role="dialog" aria-modal="true" aria-label="Copy or unstack" data-testid="copy-unstack">
		<h3>Copy {modal.member.title}, or unstack it?</h3>
		<div class="wm-explainer" aria-hidden="true">
			<div>
				<strong>Copy</strong>
				<p>Adds it to this list. It stays a version in the stack.</p>
			</div>
			<div>
				<strong>Unstack</strong>
				<p>Removes the version link. Playlists are not touched.</p>
			</div>
		</div>
		<div class="wm-actions">
			<button type="button" class="wm-primary" data-testid="copy-unstack-copy" onclick={() => void copyOut()}>
				Copy
			</button>
			<button type="button" data-testid="copy-unstack-unstack" onclick={() => void unstack()}>
				Unstack {modal.member.title}
			</button>
			<button type="button" onclick={close}>Cancel</button>
		</div>
	</div>
{/if}

<style>
	.wm {
		position: fixed;
		z-index: 12000;
		left: 50%;
		top: 40%;
		transform: translate(-50%, -50%);
		width: 560px;
		max-width: calc(100vw - 32px);
		max-height: calc(100vh - 32px);
		overflow: auto;
		box-sizing: border-box;
		padding: 1rem 1.1rem;
		background: var(--rb-panel-bg, #1a1a1a);
		border: 1px solid var(--rb-border, #444);
		border-radius: 4px;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);
		font-size: 12px;
	}
	.wm h3 {
		margin: 0 0 0.5rem;
		font-size: 13px;
		font-weight: 600;
	}
	.wm-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 6px 10px;
		margin-top: 6px;
	}
	.wm-field {
		display: flex;
		flex-direction: column;
		gap: 2px;
		text-transform: capitalize;
	}
	.wm input,
	.wm select {
		font: inherit;
		color: inherit;
		background: transparent;
		border: 1px solid var(--rb-border, #444);
		border-radius: 3px;
		padding: 3px 5px;
		text-transform: none;
	}
	.wm-list {
		display: flex;
		flex-direction: column;
		margin-top: 4px;
		max-height: 200px;
		overflow: auto;
	}
	.wm-list button {
		background: none;
		border: 0;
		color: inherit;
		font: inherit;
		text-align: left;
		padding: 3px 4px;
		cursor: pointer;
	}
	.wm-list button:hover,
	.wm-list button:focus-visible {
		background: rgba(255, 255, 255, 0.08);
	}
	.wm-dim,
	.wm-note {
		opacity: 0.65;
	}
	.wm-error {
		color: var(--rb-danger, #e66);
	}
	.wm-explainer {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 10px;
	}
	.wm-explainer div {
		border: 1px solid var(--rb-border, #444);
		border-radius: 3px;
		padding: 6px 8px;
	}
	.wm-explainer p {
		margin: 4px 0 0;
		opacity: 0.7;
	}
	.wm-actions {
		display: flex;
		gap: 0.5rem;
		justify-content: flex-end;
		margin-top: 0.75rem;
	}
	.wm-primary {
		font-weight: 600;
	}
</style>
