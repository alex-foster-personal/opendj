/** What a Shift-drop means (R-DND-2, E16). Pure, so the rule is testable alone. */
export type ShiftDropAction = 'link' | 'already' | 'nothing';

export function shiftDropAction(
	dragged: readonly string[],
	targetId: string,
	workOf: (stableId: string) => string | null
): ShiftDropAction {
	const others = dragged.filter((id) => id !== targetId);
	if (others.length === 0) return 'nothing';
	const targetWork = workOf(targetId);
	const together = targetWork !== null && others.every((id) => workOf(id) === targetWork);
	return together ? 'already' : 'link';
}
