/**
 * Guards the destructive writeback UI against stale async responses.
 *
 * A plan is valid only for its exact vendor, live database path, and native
 * playlist ID.  Requests capture that selection, then may publish only while
 * it remains current.  This deliberately does not "retarget" a valid plan.
 */
export interface WritebackSelection {
	vendor: string;
	target_mode: string;
	target_path: string;
	target_id: string;
}

export interface WritebackPlanBinding extends WritebackSelection {}

export interface WritebackResultBinding {
	vendor: string;
	target_id: string;
}

export interface WritebackPlanChange {
	ordered_match: boolean;
	added: string[];
	removed: string[];
}

interface SelectionTicket {
	generation: number;
	selection: WritebackSelection;
}

export class WritebackRequestGate {
	#generation = 0;

	capture(selection: WritebackSelection): SelectionTicket {
		this.#generation += 1;
		return { generation: this.#generation, selection: { ...selection } };
	}

	invalidate(): void {
		this.#generation += 1;
	}

	isCurrent(ticket: SelectionTicket, current: WritebackSelection | null): boolean {
		return ticket.generation === this.#generation && selectionsMatch(ticket.selection, current);
	}
}

export function selectionsMatch(
	left: WritebackSelection,
	right: WritebackSelection | null
): boolean {
	return (
		right !== null &&
		left.vendor === right.vendor &&
		left.target_mode === right.target_mode &&
		left.target_path === right.target_path &&
		left.target_id === right.target_id
	);
}

export function planMatchesSelection(
	plan: WritebackPlanBinding,
	selection: WritebackSelection | null
): boolean {
	return selectionsMatch(plan, selection);
}

export function resultMatchesSelection(
	result: WritebackResultBinding,
	selection: WritebackSelection | null
): boolean {
	return selection !== null && result.vendor === selection.vendor && result.target_id === selection.target_id;
}

export function writebackPlanMutation(plan: WritebackPlanChange): 'noop' | 'reorder' | 'membership' {
	if (plan.ordered_match) return 'noop';
	if (plan.added.length === 0 && plan.removed.length === 0) return 'reorder';
	return 'membership';
}

export function canRollbackWriteback(
	confirmed: boolean,
	plan: WritebackPlanBinding | null,
	result: WritebackResultBinding | null,
	selection: WritebackSelection | null
): boolean {
	return confirmed && plan !== null && result !== null && planMatchesSelection(plan, selection) && resultMatchesSelection(result, selection);
}
