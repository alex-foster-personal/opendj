/** Virtual-edit window fields a deck `load` command carries (LIBEDIT, issue #7881). */
export type EditWindowRow = {
	edit_id?: string | null;
	in_ms?: number | null;
	out_ms?: number | null;
};

export function deckLoadEditWindow(row: EditWindowRow): {
	edit_id?: string;
	in_ms?: number;
	out_ms?: number;
} {
	return {
		...(row.edit_id ? { edit_id: row.edit_id } : {}),
		...(row.in_ms != null ? { in_ms: row.in_ms } : {}),
		...(row.out_ms != null ? { out_ms: row.out_ms } : {})
	};
}
