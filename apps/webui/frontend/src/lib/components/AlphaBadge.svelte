<script lang="ts">
	import { RELEASE_STAGE, RELEASE_STAGE_TITLE } from '$lib/release-stage';
</script>

<span class="release-stage" data-testid="release-stage-badge" title={RELEASE_STAGE_TITLE}>
	{RELEASE_STAGE}
</span>

<style>
	/* OSSPUB-07 (the maintainer, Mon 5 Oct 2026): a quiet stage marker, not an orange
	   chip. Plain text in the theme's primary text colour, so the light skin
	   still reads; .perf-root surfaces define --rb-text, the shell defines --fg. */
	.release-stage {
		display: inline-block;
		margin-left: 4px;
		border: none;
		background: none;
		color: var(--rb-text, var(--fg));
		font-size: 0.75em;
		font-style: italic;
		font-weight: 400;
		text-transform: lowercase;
		vertical-align: middle;
		cursor: help;
	}
</style>
