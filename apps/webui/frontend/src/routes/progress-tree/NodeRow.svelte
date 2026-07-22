<script lang="ts">
	/**
	 * One ledger node as a tree row: status chip, title, effort badge,
	 * reuse hint, clickable deps, short SHAs (click-to-copy full, note on
	 * hover), tests behind a count badge, verified line, and the red
	 * 'no provenance' tag when working/verified carries zero commits.
	 */
	import StatusChip from './StatusChip.svelte';
	import { hasProvenanceGap, type ProgressNode } from './types';

	let {
		node,
		convention,
		onJump
	}: {
		node: ProgressNode;
		convention: string;
		onJump: (id: string) => void;
	} = $props();

	let testsOpen = $state(false);
	let copiedSha = $state<string | null>(null);

	async function copySha(sha: string): Promise<void> {
		await navigator.clipboard.writeText(sha);
		copiedSha = sha;
		setTimeout(() => {
			if (copiedSha === sha) copiedSha = null;
		}, 1200);
	}
</script>

<div class="node-row" id={`pt-node-${node.id}`}>
	<div class="main-line">
		<StatusChip status={node.status} />
		<span class="title" title={node.id}>{node.title}</span>
		<span class="effort effort-{node.effort}">{node.effort}</span>
		{#if node.reuse}
			<span class="reuse" title="reuse">reuse: {node.reuse}</span>
		{/if}
		{#if hasProvenanceGap(node)}
			<span class="no-provenance" title={convention}>no provenance</span>
		{/if}
		{#each node.deps as dep, i (`${dep}:${i}`)}
			<button class="dep" onclick={() => onJump(dep)} title="jump to {dep}">&#8599; {dep}</button>
		{/each}
		{#each node.commits as commit, i (`${commit.sha}:${i}`)}
			<button
				class="sha"
				title={copiedSha === commit.sha ? 'copied full SHA' : commit.note}
				onclick={() => copySha(commit.sha)}
			>
				{copiedSha === commit.sha ? 'copied' : commit.sha.slice(0, 7)}
			</button>
		{/each}
		{#if node.tests.length > 0}
			<button class="tests-badge" onclick={() => (testsOpen = !testsOpen)}>
				tests {node.tests.length}
			</button>
		{/if}
	</div>
	{#if node.notes}
		<div class="notes">{node.notes}</div>
	{/if}
	{#if node.verified}
		<div class="verified-line">
			verified by {node.verified.by} on {node.verified.date} via {node.verified.method}
		</div>
	{/if}
	{#if testsOpen}
		<ul class="tests-list">
			{#each node.tests as test, i (`${test}:${i}`)}
				<li>{test}</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.node-row {
		padding: 0.35rem 0.5rem 0.35rem 1.25rem;
		border-bottom: 1px solid var(--border);
		border-radius: 4px;
	}
	.node-row:hover {
		background: #141a24;
	}
	/* :global so page-level jumpToNode can add .flash via classList */
	:global(.node-row.flash) {
		animation: pt-flash 1.6s ease-out;
	}
	@keyframes pt-flash {
		0% {
			background: #3a3010;
		}
		100% {
			background: transparent;
		}
	}
	.main-line {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.title {
		font-size: 0.88rem;
	}
	.effort {
		font-size: 0.68rem;
		font-weight: 700;
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
		border: 1px solid var(--border);
		color: var(--muted);
	}
	.effort-L {
		color: var(--accent);
		border-color: var(--accent-dim);
	}
	.reuse {
		font-size: 0.75rem;
		color: var(--muted);
		font-style: italic;
	}
	.no-provenance {
		font-size: 0.7rem;
		font-weight: 700;
		color: #fff;
		background: var(--danger);
		padding: 0.05rem 0.4rem;
		border-radius: 4px;
		cursor: help;
	}
	.dep,
	.sha,
	.tests-badge {
		font-size: 0.72rem;
		padding: 0.05rem 0.4rem;
		border-radius: 4px;
		line-height: 1.3;
	}
	.dep {
		color: var(--accent);
		background: transparent;
		border: 1px solid var(--accent-dim);
	}
	.sha {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		color: var(--muted);
	}
	.tests-badge {
		color: #7cc0ff;
		border-color: #2c5d8f;
		background: transparent;
	}
	.notes,
	.verified-line {
		margin: 0.15rem 0 0 5.6rem;
		font-size: 0.75rem;
		color: var(--muted);
	}
	.verified-line {
		color: #4ade80;
	}
	.tests-list {
		margin: 0.2rem 0 0.1rem 5.6rem;
		padding-left: 1rem;
		font-size: 0.75rem;
		color: var(--muted);
	}
	.tests-list li {
		margin: 0.1rem 0;
	}
</style>
