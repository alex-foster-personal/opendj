/** OJ sidebar toggle rules (#9477): when the top bar's OJ button shows, and whether the panel may slide in. */
import type { AssistantStatus } from './assistant-api';
import { assistantPanelMode } from './assistant-api';
import { ojMotionPolicy } from '$lib/components/assistant/oj-motion-policy';

/** The OJ button shows only with the sidebar flag on and the assistant configured (assistantPanelMode 'chat'). */
export function ojTopBarButtonVisible(
	flagEnabled: boolean,
	status: Pick<AssistantStatus, 'configured'> | null,
	statusError: string | null
): boolean {
	return flagEnabled && assistantPanelMode(true, status, statusError) === 'chat';
}

export interface OjSidebarMotionInput {
	anyDeckPlaying: boolean;
	performanceVisible: boolean;
	reducedMotion: boolean;
}

/** The panel animates open only when ojMotionPolicy allows animated fill: never while a deck plays on /performance, never under reduced motion. */
export function ojSidebarAnimates(input: OjSidebarMotionInput): boolean {
	return ojMotionPolicy({ ...input, streaming: false }).fillMode === 'animated';
}
