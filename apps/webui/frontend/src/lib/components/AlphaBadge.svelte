<script lang="ts">
	import { RELEASE_STAGE, RELEASE_STAGE_TITLE } from '$lib/release-stage';
</script>

<span class="release-stage" data-testid="release-stage-badge" title={RELEASE_STAGE_TITLE}>
	{RELEASE_STAGE}
</span>

<style>
	.release-stage {
		display: inline-block;
		margin-left: 4px;
		padding: 0 4px;
		border: 1px solid #d97757;
		border-radius: 3px;
		color: #d97757;
		font-size: 9px;
		font-weight: 700;
		line-height: 13px;
		letter-spacing: 0.6px;
		text-transform: uppercase;
		vertical-align: middle;
		cursor: help;
	}
</style>
