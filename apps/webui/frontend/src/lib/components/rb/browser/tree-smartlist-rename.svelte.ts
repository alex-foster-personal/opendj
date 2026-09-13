/**
 * Inline smartlist rename + create-then-rename flow for TreeSmartlistSection.
 *
 * Rune class - the .svelte.ts extension is REQUIRED for $state.
 */
import { tick } from 'svelte';
import type { SmartlistRule, SmartlistSummary } from '$lib/rb/api-smartlists';
import type { TreeSmartlists } from './tree-smartlists.svelte';

const STARTER_RULE: SmartlistRule = { field: 'rating', op: '>=', value: 0 };

export class TreeSmartlistRename {
	editingId: string | null = $state(null);
	editDraft = $state('');
	inputEl: HTMLInputElement | null = $state(null);
	pendingId: string | null = $state(null);

	constructor(
		private readonly rowsOf: () => SmartlistSummary[] | null,
		private readonly smartlists: TreeSmartlists
	) {}

	async begin(sl: SmartlistSummary): Promise<void> {
		this.editingId = sl.id;
		this.editDraft = sl.name;
		await tick();
		this.inputEl?.focus();
		this.inputEl?.select();
	}

	async commit(): Promise<void> {
		const id = this.editingId;
		if (id === null) return;
		const rows = this.rowsOf();
		const sl = rows?.find((row) => row.id === id);
		this.editingId = null;
		if (sl === undefined) return;
		const next = this.editDraft.trim();
		if (next === '' || next === sl.name) return;
		await this.smartlists.rename(id, next);
	}

	cancel(): void {
		this.editingId = null;
	}

	async createAndRename(): Promise<void> {
		const created = await this.smartlists.create(this._unusedName(), STARTER_RULE);
		this.pendingId = created.id;
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

	checkPending(): void {
		const id = this.pendingId;
		if (id === null) return;
		const sl = this.rowsOf()?.find((row) => row.id === id);
		if (sl === undefined) return;
		this.pendingId = null;
		void this.begin(sl);
	}

	_unusedName(): string {
		const base = 'New Smartlist';
		const existing = new Set((this.rowsOf() ?? []).map((row) => row.name));
		if (!existing.has(base)) return base;
		let n = 2;
		while (existing.has(`${base} ${n}`)) n += 1;
		return `${base} ${n}`;
	}
}
