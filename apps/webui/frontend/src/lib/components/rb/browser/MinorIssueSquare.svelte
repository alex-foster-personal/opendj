<script lang="ts">
	import type { MinorIssue } from '$lib/rb/track-minor-issues';

	let { issues }: { issues: MinorIssue[] } = $props();

	const title = $derived(issues.map((i) => i.label).join('; '));
</script>

{#if issues.length > 0}
	<span class="minor-issue-square" title={title} aria-label={title} data-testid="minor-issue-square">
		<svg viewBox="0 0 10 10" width="9" height="9" aria-hidden="true">
			<rect x="1" y="1" width="8" height="8" rx="1" fill="var(--rb-issue-square, #e8a838)" />
			<path d="M5 2.5v3.2M5 7.2h.01" stroke="#1a1a1a" stroke-width="1.1" stroke-linecap="round" />
		</svg>
	</span>
{/if}

<style>
	.minor-issue-square {
		display: inline-flex;
		margin-left: 2px;
		vertical-align: middle;
	}
</style>
