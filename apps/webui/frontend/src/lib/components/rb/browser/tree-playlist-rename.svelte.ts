/**
 * Inline playlist rename + create-then-rename flow, split out of
 * PlaylistTree.svelte's script (a self-contained editing concern, separate
 * from the tree's row rendering). Constructed with accessor functions
 * rather than plain values so it always reads PlaylistTree's LIVE props
 * (`nodes`, `onrenameplaylist`, `oncreateplaylist` can all change over the
 * component's lifetime) instead of a stale snapshot from construction time.
 *
 * Rune class - the .svelte.ts extension is REQUIRED for $state.
 */
import { tick } from 'svelte';
import type { PlaylistNode } from '$lib/rb/library-types';

export class TreePlaylistRename {
	/** playlist_id currently being renamed inline; null = no row editing. */
	editingId: string | null = $state(null);
	editDraft = $state('');
	inputEl: HTMLInputElement | null = $state(null);
	/** Set after '+'; rename starts once the new node appears in `nodesOf()`. */
	pendingId: string | null = $state(null);

	constructor(
		private readonly nodesOf: () => PlaylistNode[],
		private readonly renameOf: () => ((node: PlaylistNode, name: string) => void | Promise<void>) | undefined,
		private readonly createOf: () => (() => Promise<string | null> | string | null) | undefined
	) {}

	async begin(node: PlaylistNode): Promise<void> {
		if (this.renameOf() === undefined) return;
		if (node.kind === 'all_tracks' || node.playlist_id === 'all') return;
		this.editingId = node.playlist_id;
		this.editDraft = node.name;
		await tick();
		this.inputEl?.focus();
		this.inputEl?.select();
	}

	async commit(): Promise<void> {
		const id = this.editingId;
		const onrenameplaylist = this.renameOf();
		if (id === null || onrenameplaylist === undefined) return;
		const node = this.nodesOf().find((n) => n.playlist_id === id);
		this.editingId = null;
		if (node === undefined) return;
		const next = this.editDraft.trim();
		if (next === '' || next === node.name) return;
		await onrenameplaylist(node, next);
	}

	cancel(): void {
		this.editingId = null;
	}

	async createAndRename(): Promise<void> {
		const oncreateplaylist = this.createOf();
		if (oncreateplaylist === undefined) return;
		const createdId = await oncreateplaylist();
		if (createdId === null || createdId === '') return;
		this.pendingId = createdId;
	}

	onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter') {
			event.preventDefault();
			void this.commit();
		} else if (event.key === 'Escape') {
			event.preventDefault();
			this.cancel();
		}
	}

	/** Call from an `$effect` in the host component: once the node a pending
	 * create-then-rename is waiting for appears in `nodesOf()`, starts the
	 * rename on it. A no-op while nothing is pending or the node hasn't
	 * landed yet. */
	checkPending(): void {
		const id = this.pendingId;
		if (id === null) return;
		const node = this.nodesOf().find((n) => n.playlist_id === id);
		if (node === undefined) return;
		this.pendingId = null;
		void this.begin(node);
	}
}
