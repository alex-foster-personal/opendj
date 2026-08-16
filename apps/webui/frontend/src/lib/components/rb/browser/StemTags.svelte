<!--
	Stems column tags: [V][I][D] = vocals / instruments (bass+other) / drums.
	Renders only what the listing hydrate measured; empty when status is none.
-->
<script lang="ts">
	import type { StemSummary } from '$lib/rb/api-rb';

	interface Props {
		stems: StemSummary | null;
	}

	const { stems }: Props = $props();

	const GROUPS = ['V', 'I', 'D'] as const;

	function _fmtBytes(n: number): string {
		if (n < 1024) return `${n}B`;
		if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)}K`;
		return `${(n / (1024 * 1024)).toFixed(1)}M`;
	}

	function _modelShort(model: string | null): string {
		if (!model) return '?';
		if (model === 'hdemucs_mmi') return 'mmi';
		if (model === 'htdemucs_ft') return 'ft';
		if (model === 'htdemucs') return 'ht';
		return model.length > 8 ? model.slice(0, 8) : model;
	}

	function _params(s: StemSummary & { status: 'ready' }): string {
		const bits: string[] = [];
		if (typeof s.overlap === 'number') bits.push(`ov${s.overlap}`);
		if (typeof s.shifts === 'number' && s.shifts > 0) bits.push(`sh${s.shifts}`);
		return bits.join(' ');
	}

	function _title(s: StemSummary | null): string {
		if (s === null) return 'stems not loaded for this row';
		if (s.status === 'none') return 'no local stem bundle';
		if (s.status === 'invalid') return `stem bundle invalid: ${s.error ?? 'unknown'}`;
		const g = GROUPS.map((k) => {
			const bytes = s.groups[k]?.bytes ?? 0;
			const parts = (s.groups[k]?.parts ?? []).join('+');
			return `${k}=${_fmtBytes(bytes)} (${parts})`;
		}).join(', ');
		const params = _params(s);
		return (
			`${s.model ?? '?'} ${s.preset ?? ''} ${params}`.trim() +
			` · ${s.format} · total ${_fmtBytes(s.total_bytes)} · ${g}`
		);
	}

	const ready = $derived(stems !== null && stems.status === 'ready' ? stems : null);
	const meta = $derived(
		ready === null
			? ''
			: `${_modelShort(ready.model)}${_params(ready) ? ' ' + _params(ready) : ''} ${ready.format} ${_fmtBytes(ready.total_bytes)}`
	);
</script>

{#if ready === null}
	<span class="stem-tags empty" title={_title(stems)} aria-label={_title(stems)}>-</span>
{:else}
	<span class="stem-tags" title={_title(ready)} aria-label={_title(ready)}>
		{#each GROUPS as g (g)}
			<span
				class="tag"
				class:on={(ready.groups[g]?.bytes ?? 0) > 0}
				data-g={g}
			>{g}</span>
		{/each}
		<span class="meta">{meta}</span>
	</span>
{/if}

<style>
	.stem-tags {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		max-width: 100%;
		min-width: 0;
		font-size: 9px;
		line-height: 14px;
		white-space: nowrap;
		overflow: hidden;
	}
	.stem-tags.empty {
		color: color-mix(in srgb, var(--rb-text, #888) 35%, transparent);
	}
	.tag {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 12px;
		height: 12px;
		border-radius: 2px;
		border: 1px solid color-mix(in srgb, var(--rb-text, #888) 22%, transparent);
		color: color-mix(in srgb, var(--rb-text, #888) 45%, transparent);
		font-weight: 700;
		font-size: 8px;
		letter-spacing: 0;
	}
	.tag.on[data-g='V'] {
		background: color-mix(in srgb, #6ec8ff 35%, transparent);
		border-color: color-mix(in srgb, #6ec8ff 70%, transparent);
		color: #cfefff;
	}
	.tag.on[data-g='I'] {
		background: color-mix(in srgb, #9ad67a 35%, transparent);
		border-color: color-mix(in srgb, #9ad67a 70%, transparent);
		color: #e4f5d8;
	}
	.tag.on[data-g='D'] {
		background: color-mix(in srgb, #e0a35c 35%, transparent);
		border-color: color-mix(in srgb, #e0a35c 70%, transparent);
		color: #ffe7c8;
	}
	.meta {
		margin-left: 3px;
		color: var(--rb-text-dim, #7a8088);
		overflow: hidden;
		text-overflow: ellipsis;
		min-width: 0;
	}
</style>
