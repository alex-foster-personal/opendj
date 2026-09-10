/**
 * Pure assignment and copy for auto-cue proposals in empty hot-cue slots.
 * No runes, no fetch. Slot mapping is fixed-index: proposals[i] belongs to
 * SLOTS[i] and does not slide into a later letter when an earlier slot is
 * already filled.
 */
import type { AutoCueOut } from './auto-cues-api';
import type { HotCueSlot } from './hot-cue-types';

export const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

const ALLOWED_KINDS = new Set(['intro', 'drop', 'break', 'outro', '']);

export function visibleProposalForSlot(
	slot: HotCueSlot,
	filledSlots: ReadonlySet<HotCueSlot>,
	proposals: AutoCueOut[]
): AutoCueOut | null {
	if (filledSlots.has(slot)) return null;
	const index = HOT_CUE_SLOTS.indexOf(slot);
	if (index < 0 || index > 7) return null;
	const proposal = proposals[index];
	if (proposal === undefined) return null;
	if (!ALLOWED_KINDS.has(proposal.kind)) return null;
	return proposal;
}

export function proposalCaption(kind: string): string {
	if (kind === '' || !ALLOWED_KINDS.has(kind)) return 'proposed';
	return `proposed ${kind}`;
}

export function formatProposalTime(time_s: number): string {
	const total = Math.floor(time_s);
	const minutes = Math.floor(total / 60);
	const seconds = total % 60;
	return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export function proposalTitle(kind: string, time_s: number): string {
	const at = formatProposalTime(time_s);
	if (kind === '' || !ALLOWED_KINDS.has(kind)) return `proposed cue at ${at} - not saved`;
	return `proposed ${kind} at ${at} - not saved`;
}
