/** Shared open state of the OJ (agent) sidebar (#9477): the right-edge tab, Cmd+I, the top bar OJ button and the `oj_toggle` command all flip this one value. */
import { settleFocusOnClose } from './sidebar-toggle';

export const ojSidebar = $state<{ open: boolean }>({ open: false });

/** What had focus when the sidebar opened; closing hands focus back to it. */
let opener: HTMLElement | null = null;

function activeElement(): HTMLElement | null {
	if (typeof document === 'undefined') return null;
	return document.activeElement instanceof HTMLElement ? document.activeElement : null;
}

export function setOjSidebarOpen(next: boolean): void {
	if (next === ojSidebar.open) return;
	if (next) opener = activeElement();
	ojSidebar.open = next;
	if (!next) {
		settleFocusOnClose(opener, activeElement);
		opener = null;
	}
}

export function toggleOjSidebar(): void {
	setOjSidebarOpen(!ojSidebar.open);
}
