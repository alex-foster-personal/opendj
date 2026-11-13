<script lang="ts">
	// LIBM-104: Rename and Delete on a system node, shown dimmed and refused.
	// aria-disabled (never `disabled`) keeps both focusable, so the reason in
	// the title and in aria-describedby is reachable by keyboard and screen reader.
	import { CLOSE_PATH, PENCIL_PATH } from '$lib/ui/icon-glyphs';
	import {
		invokeSystemNodeAction,
		systemNodeLockReason,
		type SystemNodeAction
	} from './system-tree-nodes';

	let { name, nodeId }: { name: string; nodeId: string } = $props();

	const reason = $derived(systemNodeLockReason(name));
	const reasonId = $derived(`system-node-reason-${nodeId}`);

	function _refuse(event: Event, action: SystemNodeAction): void {
		event.preventDefault();
		event.stopPropagation();
		invokeSystemNodeAction(action, name);
	}

	function _keydown(event: KeyboardEvent, action: SystemNodeAction): void {
		if (event.key === 'Enter' || event.key === ' ') _refuse(event, action);
	}
</script>

<span id={reasonId} class="sr-only">{reason}</span>
<button
	type="button"
	class="sys-action"
	aria-disabled="true"
	aria-label="Rename"
	aria-describedby={reasonId}
	title={reason}
	data-testid="system-node-rename"
	onclick={(e) => _refuse(e, 'rename')}
	onkeydown={(e) => _keydown(e, 'rename')}
>
	<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">
		<path d={PENCIL_PATH} fill="none" stroke="currentColor" stroke-width="2" />
	</svg>
</button>
<button
	type="button"
	class="sys-action"
	aria-disabled="true"
	aria-label="Delete"
	aria-describedby={reasonId}
	title={reason}
	data-testid="system-node-delete"
	onclick={(e) => _refuse(e, 'delete')}
	onkeydown={(e) => _keydown(e, 'delete')}
>
	<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">
		<path d={CLOSE_PATH} fill="none" stroke="currentColor" stroke-width="2" />
	</svg>
</button>

<style>
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		padding: 0;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
		border: 0;
	}
	.sys-action {
		flex: none;
		border: none;
		background: transparent;
		color: var(--rb-text-dim);
		font-size: 12px;
		line-height: 1;
		padding: 0 3px;
		cursor: not-allowed;
		opacity: 0.35;
	}
	.sys-action:focus-visible {
		opacity: 0.7;
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 1px;
	}
</style>
