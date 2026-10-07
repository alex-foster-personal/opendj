/**
 * Bind resolved registry items to a host's runners (W28, CTXMENU-05).
 *
 * The registry decides WHAT shows and WHETHER it is enabled. A host only says
 * HOW to run each enabled item, keyed by item id. An enabled item whose host
 * has no runner is rendered disabled with a reason, never silently dropped
 * and never left clickable with nothing behind it.
 */
import type { ResolvedMenuItem } from './registry';

export type MenuRunners = Record<string, (() => void | Promise<void>) | undefined>;

export interface BoundMenuItem {
	id: string;
	label: string;
	run?: (() => void | Promise<void>) | undefined;
	title: string;
	testId?: string;
	checked?: boolean;
}

export const NOT_WIRED_HERE = 'not available in this view';

export function bindMenuItems(items: readonly ResolvedMenuItem[], runners: MenuRunners): BoundMenuItem[] {
	return items.map((item) => {
		const runner = item.enabled ? runners[item.id] : undefined;
		const bound: BoundMenuItem = {
			id: item.id,
			label: item.label,
			title: item.enabled && runner === undefined ? `${NOT_WIRED_HERE} - ${item.title}` : item.title
		};
		if (runner !== undefined) bound.run = runner;
		if (item.testId !== undefined) bound.testId = item.testId;
		if (item.checked !== undefined) bound.checked = item.checked;
		return bound;
	});
}
