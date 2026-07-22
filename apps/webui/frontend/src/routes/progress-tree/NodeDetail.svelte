<script lang="ts">
	/**
	 * Shared fold-out detail block for a ledger node. Rendered inside a tree
	 * row's expansion and inside the graph tab's side panel, so both surfaces
	 * show identical detail: full notes as bullet segments, clickable dep
	 * chips, reverse-dep ("blocks") chips, every commit (7-char mono SHA
	 * click-to-copy + note), build metadata, the test list, links
	 * (issues/specs/refs), and the verified by/date/method line.
	 */
	import {
		BUILD_STATE_GLYPH,
		BUILD_STATE_LABEL,
		GITHUB_MARK_PATH,
		githubIssueUrl,
		noteSegments,
		staleBuildLabel,
		type ProgressNode
	} from './types';

	let {
		node,
		blocks = [],
		onJump
	}: {
		node: ProgressNode;
		blocks?: string[];
		onJump: (id: string) => void;
	} = $props();

	let copiedSha = $state<string | null>(null);
	let copiedSpec = $state<string | null>(null);

	const segments = $derived(noteSegments(node.notes));
	const staleLabel = $derived(staleBuildLabel(node.build?.updated ?? null));

	async function copySha(sha: string): Promise<void> {
		await navigator.clipboard.writeText(sha);
		copiedSha = sha;
		setTimeout(() => {
			if (copiedSha === sha) copiedSha = null;
		}, 1200);
	}

	async function copySpec(spec: string): Promise<void> {
		await navigator.clipboard.writeText(spec);
		copiedSpec = spec;
		setTimeout(() => {
			if (copiedSpec === spec) copiedSpec = null;
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

	{#if blocks.length > 0}
		<div class="row">
			<span class="label">blocks</span>
			<div class="chips">
				{#each blocks as id, i (`${id}:${i}`)}
					<button class="dep blocks" onclick={() => onJump(id)} title="jump to {id}">
						&#8599; {id}
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

	{#if node.build}
		{@const build = node.build}
		<div class="row">
			<span class="label">build</span>
			<div class="build-info">
				{#if build.state}
					<span class="build-glyph state-{build.state}" title="build state: {BUILD_STATE_LABEL[build.state]}">
						{BUILD_STATE_GLYPH[build.state]}
					</span>
				{/if}
				{#if build.stage}<span class="build-stage">{build.stage}</span>{/if}
				{#if staleLabel}
					<span class="stale-tag" title="build.updated: {build.updated}">{staleLabel}</span>
				{/if}
			</div>
			<div class="chips">
				{#if build.branch}<span class="mono-chip" title="branch">{build.branch}</span>{/if}
				{#if build.pr}<span class="mono-chip" title="PR">#{build.pr}</span>{/if}
				{#if build.worktree}<span class="mono-chip" title="worktree">{build.worktree}</span>{/if}
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

	{#if node.links}
		{@const links = node.links}
		{#if links.issues.length > 0}
			<div class="row">
				<span class="label">issues</span>
				<div class="chips">
					{#each links.issues as issue, i (`${issue}:${i}`)}
						<a
							class="link-chip gh"
							href={githubIssueUrl(issue)}
							target="_blank"
							rel="noopener noreferrer"
							title="GitHub #{issue}"
						>
							<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"
								><path d={GITHUB_MARK_PATH} fill="currentColor" /></svg
							>
							#{issue}
						</a>
					{/each}
				</div>
			</div>
		{/if}
		{#if links.specs.length > 0}
			<div class="row">
				<span class="label">specs</span>
				<div class="chips">
					{#each links.specs as spec, i (`${spec}:${i}`)}
						<button
							class="link-chip doc"
							title={copiedSpec === spec ? 'copied path' : spec}
							onclick={() => copySpec(spec)}
						>
							<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
								<path
									d="M3 1.5h6l3 3v10a.5.5 0 0 1-.5.5h-8a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5z"
									fill="none"
									stroke="currentColor"
									stroke-width="1"
								/>
								<path d="M9 1.5V4.5H12" fill="none" stroke="currentColor" stroke-width="1" />
								<line x1="5" y1="8" x2="11" y2="8" stroke="currentColor" stroke-width="1" />
								<line x1="5" y1="10.5" x2="11" y2="10.5" stroke="currentColor" stroke-width="1" />
							</svg>
							{copiedSpec === spec ? 'copied' : spec.split('/').pop()}
						</button>
					{/each}
				</div>
			</div>
		{/if}
		{#if links.refs.length > 0}
			<div class="row">
				<span class="label">refs</span>
				<div class="chips">
					{#each links.refs as ref, i (`${ref}:${i}`)}
						<a class="link-chip ref" href={ref} target="_blank" rel="noopener noreferrer" title={ref}>
							&#8599; ref
						</a>
					{/each}
				</div>
			</div>
		{/if}
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
	.dep.blocks {
		color: #ffb43a;
		border-color: #7a5a1f;
	}
	.build-info {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		font-size: 0.76rem;
		color: var(--fg);
		padding-top: 0.15rem;
	}
	.build-glyph {
		font-size: 0.85rem;
		line-height: 1;
		cursor: help;
	}
	.build-glyph.state-active {
		color: #4ade80;
		animation: bi-pulse 1.6s ease-in-out infinite;
	}
	.build-glyph.state-idle {
		color: #7cc0ff;
	}
	.build-glyph.state-blocked {
		color: #ffb43a;
	}
	.build-glyph.state-hanging {
		color: var(--danger);
	}
	@keyframes bi-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.4;
		}
	}
	.build-stage {
		color: var(--muted);
		font-style: italic;
	}
	.stale-tag {
		font-size: 0.68rem;
		color: var(--muted);
		opacity: 0.75;
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 0.02rem 0.35rem;
		cursor: help;
	}
	.mono-chip {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.72rem;
		padding: 0.1rem 0.4rem;
		border-radius: 4px;
		border: 1px solid var(--border);
		color: var(--muted);
	}
	.link-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: 0.72rem;
		padding: 0.1rem 0.45rem;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--fg);
		text-decoration: none;
		cursor: pointer;
		max-width: 16rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.link-chip.gh:hover,
	.link-chip.ref:hover {
		border-color: var(--accent-dim);
		color: var(--accent);
	}
	.link-chip svg {
		flex: 0 0 auto;
	}
</style>
