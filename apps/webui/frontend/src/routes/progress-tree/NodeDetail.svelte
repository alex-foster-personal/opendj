<script lang="ts">
	/**
	 * Shared fold-out detail block for a ledger node. Rendered inside a tree
	 * row's expansion and inside the graph tab's side panel, so both surfaces
	 * show identical detail: full notes as bullet segments, clickable dep
	 * chips, every commit (7-char mono SHA click-to-copy + note), the test
	 * list, and the verified by/date/method line.
	 */
	import { noteSegments, type ProgressNode } from './types';

	let {
		node,
		onJump
	}: {
		node: ProgressNode;
		onJump: (id: string) => void;
	} = $props();

	let copiedSha = $state<string | null>(null);

	const segments = $derived(noteSegments(node.notes));

	async function copySha(sha: string): Promise<void> {
		await navigator.clipboard.writeText(sha);
		copiedSha = sha;
		setTimeout(() => {
			if (copiedSha === sha) copiedSha = null;
		}, 1200);
	}
</script>

<div class="detail">
	{#if segments.length > 0}
		<ul class="notes">
			{#each segments as seg, i (`${i}`)}
				<li>{seg}</li>
			{/each}
		</ul>
	{/if}

	{#if node.deps.length > 0}
		<div class="row">
			<span class="label">deps</span>
			<div class="chips">
				{#each node.deps as dep, i (`${dep}:${i}`)}
					<button class="dep" onclick={() => onJump(dep)} title="jump to {dep}">
						&#8599; {dep}
					</button>
				{/each}
			</div>
		</div>
	{/if}

	{#if node.commits.length > 0}
		<div class="row">
			<span class="label">commits</span>
			<div class="chips">
				{#each node.commits as commit, i (`${commit.sha}:${i}`)}
					<button
						class="sha"
						title={copiedSha === commit.sha ? 'copied full SHA' : 'click to copy full SHA'}
						onclick={() => copySha(commit.sha)}
					>
						<code>{copiedSha === commit.sha ? 'copied ' : commit.sha.slice(0, 7)}</code>
						<span class="commit-note">{commit.note}</span>
					</button>
				{/each}
			</div>
		</div>
	{/if}

	{#if node.tests.length > 0}
		<div class="row">
			<span class="label">tests</span>
			<ul class="tests">
				{#each node.tests as test, i (`${test}:${i}`)}
					<li>{test}</li>
				{/each}
			</ul>
		</div>
	{/if}

	{#if node.verified}
		<div class="verified-line">
			verified by {node.verified.by} on {node.verified.date} via {node.verified.method}
		</div>
	{/if}
</div>

<style>
	.detail {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		padding: 0.5rem 0.5rem 0.6rem 0.5rem;
	}
	.notes {
		margin: 0;
		padding-left: 1.1rem;
		font-size: 0.78rem;
		color: var(--fg);
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	.notes li {
		line-height: 1.35;
	}
	.row {
		display: flex;
		gap: 0.5rem;
		align-items: flex-start;
	}
	.label {
		flex: 0 0 3.4rem;
		font-size: 0.68rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted);
		padding-top: 0.15rem;
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
	}
	.dep {
		font-size: 0.72rem;
		padding: 0.1rem 0.45rem;
		border-radius: 4px;
		color: var(--accent);
		background: transparent;
		border: 1px solid var(--accent-dim);
	}
	.sha {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
		font-size: 0.74rem;
		padding: 0.1rem 0.45rem;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--fg);
		max-width: 100%;
		text-align: left;
	}
	.sha code {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		color: var(--muted);
		flex: 0 0 auto;
	}
	.commit-note {
		color: var(--muted);
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.tests {
		margin: 0;
		padding-left: 1.1rem;
		font-size: 0.76rem;
		color: var(--muted);
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.verified-line {
		font-size: 0.76rem;
		color: #4ade80;
	}
</style>
