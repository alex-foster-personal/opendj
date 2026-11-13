import { TIER_TITLE } from '$lib/assistant/agent-labels';
import type { Item } from '$lib/assistant/agent-session';
import type { Decision } from './AgentSidebarView.svelte';

export function shortModel(model: string | null): string {
	return model ? (model.split('/').at(-1) ?? model) : '';
}

export function tierTitle(item: Extract<Item, { kind: 'tier' }>): string {
	return `${TIER_TITLE[item.tier]}. ${item.reason}. Model: ${item.model ?? 'none'}`;
}

export function argSummary(args: Record<string, unknown>): string {
	return Object.values(args)
		.map((value) => (typeof value === 'string' ? value : JSON.stringify(value)))
		.join(' ');
}

export function pretty(value: unknown): string {
	const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
	return text.length > 2000 ? `${text.slice(0, 2000)}\n...` : text;
}

export function pendingLabel(decision: Decision | undefined): string | null {
	if (decision === 'approve') return 'Approved - waiting';
	if (decision === 'decline') return 'Declined - waiting';
	return null;
}
