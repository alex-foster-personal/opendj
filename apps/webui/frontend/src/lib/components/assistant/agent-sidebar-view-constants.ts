import type { ToolStatus } from '$lib/assistant/agent-session';
import type { TierChoice } from './AgentSidebarView.svelte';

export const TIER_CHOICES: { value: TierChoice; label: string; title: string }[] = [
	{ value: 'auto', label: 'Auto', title: 'Pick Instant, Fast or Think for each message' },
	{ value: 'fast', label: 'Fast', title: 'Always use the fast model' },
	{ value: 'think', label: 'Think', title: 'Always use the planning model' }
];

export const TOOL_STATUS_LABEL: Record<ToolStatus, string> = {
	running: 'Running',
	held: 'Needs your OK',
	ok: 'Done',
	failed: 'Failed',
	declined: 'Declined'
};
