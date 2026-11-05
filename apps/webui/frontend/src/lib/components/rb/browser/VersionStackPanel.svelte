<script lang="ts">
	// R-VER-3: the expanded stack. Anchored under its row, over the rows below,
	// because the table virtualizes on a constant row height: adding height
	// would break the window maths, and adding rows would change the track
	// count, which R-VER-1 forbids.
	import { addPlaylistItems } from '$lib/rb/playlist-write';
	import {
		dismissSuggestion,
		fetchStack,
		linkTracks,
		moveMembers,
		reportWorksFailure,
		undoLast,
		unlinkMember,
		worksView,
		type StackMember,
		type StackView,
		type Suggestion
	} from '$lib/rb/works-overlay.svelte';

	let {
		stableId,
		playlistId,
		rowPosition,
		top,
		onclose
	}: {
		stableId: string;
		playlistId: string | null;
		/** The opening row's 1-based order: as a 0-based insert index it is the slot right under it. */
		rowPosition: number | undefined;
		top: number;
		onclose: () => void;
	} = $props();

	let view = $state<StackView | null>(null);
	let failure = $state<string | null>(null);
	let dragging = $state<string | null>(null);
	let panel = $state<HTMLElement | null>(null);

	// The table pane can be a few rows tall: an anchored panel must not open out of sight.
	$effect(() => {
		if (view !== null) panel?.scrollIntoView({ block: 'nearest' });
	});

	async function reload(): Promise<void> {
		try {
			view = await fetchStack(stableId, playlistId);
			failure = null;
		} catch (exc) {
			failure = exc instanceof Error ? exc.message : String(exc);
		}
	}

	$effect(() => {
		void stableId;
		void playlistId;
		void worksView.overlay;
		void reload();
	});

	function onDragOverMember(event: DragEvent, target: StackMember): void {
		if (dragging === null || view === null || dragging === target.member_id) return;
		event.preventDefault();
		const ids = view.members.map((m) => m.member_id);
		const from = ids.indexOf(dragging);
		const to = ids.indexOf(target.member_id);
		const next = [...view.members];
		next.splice(to, 0, ...next.splice(from, 1));
		view = { ...view, members: next };
	}

	async function onDropMember(event: DragEvent): Promise<void> {
		event.preventDefault();
		event.stopPropagation();
		const work = view?.work;
		dragging = null;
		worksView.stackDrag = null;
		if (work && view) {
			await moveMembers(work.id, view.members.map((m) => m.member_id)).catch((exc: unknown) =>
				reportWorksFailure('reorder failed', exc)
			);
		}
	}

	function onDragStart(event: DragEvent, member: StackMember): void {
		dragging = member.member_id;
		if (view?.work) worksView.stackDrag = { member, workId: view.work.id };
		event.dataTransfer?.setData('text/plain', member.member_id);
		if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
	}

	function onDragEnd(): void {
		dragging = null;
		worksView.stackDrag = null;
	}

	async function addBelow(member: StackMember): Promise<void> {
		if (playlistId === null) return;
		await addPlaylistItems(playlistId, [member.member_id], rowPosition).catch((exc: unknown) =>
			reportWorksFailure('add below failed', exc)
		);
		await reload();
	}

	async function unlink(member: StackMember): Promise<void> {
		if (!view?.work) return;
		await unlinkMember(view.work.id, member.member_id).catch((exc: unknown) =>
			reportWorksFailure('unlink failed', exc)
		);
	}

	async function accept(suggestion: Suggestion): Promise<void> {
		const ids = suggestion.member_id === null ? [stableId] : [stableId, suggestion.member_id];
		const title = suggestion.work_title ?? view?.members[0]?.title ?? suggestion.title ?? undefined;
		await linkTracks(ids, {
			...(suggestion.work_id === null ? {} : { workId: suggestion.work_id }),
			...(title === undefined ? {} : { title })
		}).catch((exc: unknown) => reportWorksFailure('link failed', exc));
	}

	async function dismiss(suggestion: Suggestion): Promise<void> {
		const other = suggestion.member_id ?? `work:${suggestion.work_id}`;
		await dismissSuggestion(stableId, other).catch((exc: unknown) =>
			reportWorksFailure('dismiss failed', exc)
		);
		await reload();
	}

	async function undo(): Promise<void> {
		await undoLast().catch((exc: unknown) => reportWorksFailure('undo refused', exc));
	}

	function placeText(member: StackMember): string {
		if (playlistId === null) return '';
		return member.place === null ? 'not in this list' : `#${member.place} here`;
	}
</script>

<div
	bind:this={panel}
	class="stack"
	style={`top:${top}px`}
	role="region"
	aria-label="Versions of this track"
	data-testid="version-stack"
>
	{#if failure !== null}
		<p class="stack-error" data-testid="version-stack-error">{failure}</p>
	{:else if view !== null}
		{#if view.work !== null}
			<div class="stack-head">{view.work.title}</div>
		{/if}
		{#each view.members as member (member.member_id)}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="stack-row"
				class:dragging={dragging === member.member_id}
				data-testid="version-stack-member"
				draggable="true"
				ondragstart={(e) => onDragStart(e, member)}
				ondragover={(e) => onDragOverMember(e, member)}
				ondrop={onDropMember}
				ondragend={onDragEnd}
			>
				<span class="stack-title">{member.title ?? member.member_id}</span>
				<span class="stack-dim">{member.artist}</span>
				<span class="stack-dim">{member.role_label}</span>
				<span class="stack-dim">
					{#if member.this_row}this row{:else}{placeText(member)}{/if}
				</span>
				{#if playlistId !== null && member.place === null}
					<button type="button" class="stack-link" onclick={() => void addBelow(member)}>
						+ add below
					</button>
				{/if}
				<button type="button" class="stack-link" onclick={() => void unlink(member)}>Unlink</button>
			</div>
		{/each}
		{#each view.suggestions as suggestion (`${suggestion.member_id}:${suggestion.work_id}`)}
			<div class="stack-row suggestion" data-testid="version-stack-suggestion">
				<span class="stack-title">{suggestion.title ?? suggestion.work_title}</span>
				<span class="stack-dim">{suggestion.artist ?? ''}</span>
				<span class="stack-dim">{suggestion.reason}</span>
				<span class="stack-dim">{suggestion.confidence}</span>
				<button type="button" class="stack-link" onclick={() => void accept(suggestion)}>
					Link as version
				</button>
				<button type="button" class="stack-link" onclick={() => void dismiss(suggestion)}>
					Not a version
				</button>
			</div>
		{/each}
		<div class="stack-foot">
			{#if worksView.lastAction !== null}
				<button type="button" class="stack-link" onclick={() => void undo()}>
					Undo {worksView.lastAction}
				</button>
			{/if}
			<button type="button" class="stack-link" onclick={onclose}>Close</button>
		</div>
	{/if}
</div>

<style>
	.stack {
		position: absolute;
		left: 0;
		right: 0;
		z-index: 6;
		padding: 4px 8px;
		background: var(--rb-panel-bg, #1a1a1a);
		border-top: 1px solid var(--rb-border, #444);
		border-bottom: 1px solid var(--rb-border, #444);
		box-shadow: 0 6px 16px rgba(0, 0, 0, 0.4);
		font-size: 11px;
	}
	.stack-head {
		padding: 2px 0;
		opacity: 0.7;
	}
	.stack-row {
		display: grid;
		grid-template-columns: minmax(0, 2fr) minmax(0, 1fr) minmax(0, 1.5fr) 90px auto auto;
		gap: 8px;
		align-items: center;
		min-height: 22px;
	}
	.stack-row[draggable='true'] {
		cursor: grab;
	}
	.stack-row.dragging {
		opacity: 0.5;
	}
	.stack-title,
	.stack-dim {
		overflow: hidden;
		white-space: nowrap;
		text-overflow: ellipsis;
	}
	.stack-dim {
		opacity: 0.6;
	}
	.stack-link {
		background: none;
		border: 0;
		padding: 0 2px;
		color: inherit;
		font: inherit;
		opacity: 0.75;
		cursor: pointer;
		white-space: nowrap;
	}
	.stack-link:hover,
	.stack-link:focus-visible {
		opacity: 1;
		text-decoration: underline;
	}
	.stack-foot {
		display: flex;
		justify-content: flex-end;
		gap: 12px;
		padding-top: 2px;
	}
	.stack-error {
		margin: 2px 0;
		color: var(--rb-danger, #e66);
	}
</style>
