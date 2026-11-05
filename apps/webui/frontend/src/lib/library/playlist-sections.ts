/**
 * Where playlist sections sit in the track table (Library Organization PRD
 * s6.3, R-SEC-1..3, R-DND-1). Pure, so the veto "a drop lands somewhere other
 * than where the gap showed" is unit tested without a browser.
 *
 * The table renders SLOTS, one fixed height each: every visible row and every
 * divider. `rowIndex` keeps meaning an index into the table's rows, so only
 * the window and scroll maths deal in slots. A section's `before_position` is
 * a 0-based membership index, and `row.order` is 1-based, so a row sits at or
 * below a section when `row.order - 1 >= before_position`.
 */
import type { VirtualWindow } from '$lib/components/rb/browser/virtual-window';

/** A blank title commits as this, so no section is ever untitled (D46). */
export const DEFAULT_SECTION_TITLE = 'New section';

export interface PlaylistSection {
	id: string;
	title: string;
	before_position: number;
}

export interface SectionHeader {
	section: PlaylistSection;
	/** The table row this divider renders above; rows.length means the end. */
	beforeIndex: number;
	/** The slot this divider fills. */
	slot: number;
	/** Tracks in the section, hidden ones included. */
	trackCount: number;
	collapsed: boolean;
}

export interface SectionArrangement<R> {
	rows: R[];
	headers: SectionHeader[];
}

/** Which line a drop lands above. Neither field means the end of the list. */
export interface DropTarget {
	before_item_id?: string;
	before_section_id?: string;
}

/** Dividers show in playlist order, or sorted with "Sort within sections" on
 * (D7). A search shows matches, so dividers stay out of its way. */
export function sectionsVisible(params: {
	count: number;
	sorted: boolean;
	sortWithinSections: boolean;
	searching: boolean;
}): boolean {
	const { count, sorted, sortWithinSections, searching } = params;
	return count > 0 && !searching && (!sorted || sortWithinSections);
}

function _groupOf(sections: readonly PlaylistSection[], order: number): number {
	let group = -1;
	for (let i = 0; i < sections.length; i += 1) {
		if (sections[i].before_position > order - 1) break;
		group = i;
	}
	return group;
}

/** Split rows under their dividers and drop the ones a collapsed section
 * hides. Rows stay in the order given within each section, so handing this
 * globally sorted rows is what sorts within sections. `sections` come from the
 * server top to bottom. */
export function arrangeSections<R extends { order: number }>(
	rows: readonly R[],
	sections: readonly PlaylistSection[],
	collapsed: ReadonlySet<string>
): SectionArrangement<R> {
	const groups: R[][] = Array.from({ length: sections.length + 1 }, () => []);
	for (const row of rows) groups[_groupOf(sections, row.order) + 1].push(row);
	const arranged: R[] = [...groups[0]];
	const headers: SectionHeader[] = [];
	sections.forEach((section, index) => {
		const members = groups[index + 1];
		const isCollapsed = collapsed.has(section.id);
		headers.push({
			section,
			beforeIndex: arranged.length,
			slot: arranged.length + index,
			trackCount: members.length,
			collapsed: isCollapsed
		});
		if (!isCollapsed) arranged.push(...members);
	});
	return { rows: arranged, headers };
}

/** The membership ids of a section's tracks, hidden ones included. */
export function sectionItemIds<R extends { order: number; item_id: string | null }>(
	rows: readonly R[],
	sections: readonly PlaylistSection[],
	sectionId: string
): (string | null)[] {
	const index = sections.findIndex((section) => section.id === sectionId);
	if (index < 0) throw new Error(`sectionItemIds: unknown section ${sectionId}`);
	return [...rows]
		.sort((a, b) => a.order - b.order)
		.filter((row) => _groupOf(sections, row.order) === index)
		.map((row) => row.item_id);
}

export function slotCountOf(headers: readonly SectionHeader[], rowCount: number): number {
	return rowCount + headers.length;
}

/** The slot a table row fills. */
export function slotOfRow(headers: readonly SectionHeader[], rowIndex: number): number {
	let above = 0;
	while (above < headers.length && headers[above].beforeIndex <= rowIndex) above += 1;
	return rowIndex + above;
}

/** Rows that fill slots above `slot`. */
export function rowsAboveSlot(
	headers: readonly SectionHeader[],
	slot: number,
	rowCount: number
): number {
	for (let i = 0; i < headers.length; i += 1) {
		if (headers[i].slot >= slot) return slot - i;
	}
	return Math.min(rowCount, slot - headers.length);
}

/** Turn a slot window into the row window the table renders. A divider always
 * renders with the row below it, so the pads are exact and the scrollbar never
 * jumps as a divider crosses the window's edge. */
export function rowWindow(
	slots: VirtualWindow,
	headers: readonly SectionHeader[],
	rowCount: number,
	rowHeight: number
): VirtualWindow {
	if (headers.length === 0) return slots;
	const startIndex = rowsAboveSlot(headers, slots.startIndex, rowCount);
	const endIndex = Math.min(rowCount, rowsAboveSlot(headers, slots.endIndex, rowCount));
	const firstSlot = startIndex + headers.filter((h) => h.beforeIndex < startIndex).length;
	const total = slotCountOf(headers, rowCount);
	const endSlot =
		endIndex === rowCount
			? total
			: endIndex + headers.filter((h) => h.beforeIndex < endIndex).length;
	return {
		startIndex,
		endIndex,
		topPad: firstSlot * rowHeight,
		bottomPad: (total - endSlot) * rowHeight
	};
}

/** The gap nearest a pointer `y` pixels below the first slot's top. Gap g
 * opens above slot g; the last gap is below the last slot. */
export function gapFromPointer(y: number, rowHeight: number, slotCount: number): number {
	return Math.max(0, Math.min(slotCount, Math.round(y / rowHeight)));
}

/** The line directly below gap `gap`. This is the one definition of where a
 * drop lands, and the gap is drawn from the same slot numbers. */
export function dropTargetAt(
	gap: number,
	headers: readonly SectionHeader[],
	rows: readonly { item_id: string | null }[]
): DropTarget {
	const above = headers.filter((header) => header.slot < gap).length;
	const header = headers.find((candidate) => candidate.slot === gap);
	if (header !== undefined) return { before_section_id: header.section.id };
	const row = rows[gap - above];
	if (row === undefined) return {};
	if (row.item_id === null) {
		throw new Error(`dropTargetAt: the row below gap ${gap} has no membership id`);
	}
	return { before_item_id: row.item_id };
}
