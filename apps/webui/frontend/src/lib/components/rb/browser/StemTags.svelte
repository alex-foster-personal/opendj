<!--
	Stems column tags: [V][I][D] = vocals / instruments (bass+other) / drums.
	Renders only what the listing hydrate measured; empty when status is none.
-->
<script lang="ts">
	import type { StemSummary } from '$lib/rb/api-rb';
	import { stemCssColor } from '$lib/rb/stem-colors';
	import ControlExplainer from '../deck/ControlExplainer.svelte';

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

	function _heading(s: StemSummary | null): string {
		if (s === null || s.status === 'none') return 'STEMS unavailable';
		if (s.status === 'invalid') return 'STEMS invalid';
		return 'STEMS ready';
	}

	function _bullets(s: StemSummary | null): string[] {
		if (s === null) return ['Status: not loaded for this row'];
		if (s.status === 'none') return ['Status: no local stem bundle'];
		if (s.status === 'invalid') return [`Status: invalid - ${s.error ?? 'unknown error'}`];
		return [
			'Status: ready',
			`Model: ${s.model ?? '?'}`,
			`Preset: ${s.preset ?? '?'}`,
			`Format: ${s.format}`,
			`Total: ${_fmtBytes(s.total_bytes)}`,
			...GROUPS.map((group) => {
				const stem = s.groups[group];
				const parts = stem?.parts.join('+') || 'no parts';
				return `${group}: ${parts} (${_fmtBytes(stem?.bytes ?? 0)})`;
			})
		];
	}

	const ready = $derived(stems !== null && stems.status === 'ready' ? stems : null);
	const meta = $derived(
		ready === null
			? ''
			: `${_modelShort(ready.model)}${_params(ready) ? ' ' + _params(ready) : ''} ${ready.format} ${_fmtBytes(ready.total_bytes)}`
	);

	const colorStyle =
		`--stem-vocal: ${stemCssColor('vocal')}; ` +
		`--stem-instrumental: ${stemCssColor('instrumental')}; ` +
		`--stem-drums: ${stemCssColor('drums')};`;
</script>

{#if ready === null}
	<ControlExplainer title={_heading(stems)} bullets={_bullets(stems)}>
		<span class="stem-tags empty" tabindex="0" title={_title(stems)} aria-label={_title(stems)}>-</span>
	</ControlExplainer>
{:else}
	<ControlExplainer title={_heading(ready)} bullets={_bullets(ready)}>
		<span class="stem-tags" style={colorStyle} tabindex="0" title={_title(ready)} aria-label={_title(ready)}>
			{#each GROUPS as g (g)}
				<span
					class="tag"
					class:on={(ready.groups[g]?.bytes ?? 0) > 0}
					data-g={g}
				>{g}</span>
			{/each}
			<span class="meta">{meta}</span>
		</span>
	</ControlExplainer>
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
		background: color-mix(in srgb, var(--stem-vocal) 35%, transparent);
		border-color: color-mix(in srgb, var(--stem-vocal) 70%, transparent);
		color: #cfefff;
	}
	.tag.on[data-g='I'] {
		background: color-mix(in srgb, var(--stem-instrumental) 35%, transparent);
		border-color: color-mix(in srgb, var(--stem-instrumental) 70%, transparent);
		color: #e4f5d8;
	}
	.tag.on[data-g='D'] {
		background: color-mix(in srgb, var(--stem-drums) 35%, transparent);
		border-color: color-mix(in srgb, var(--stem-drums) 70%, transparent);
		color: #ffe7c8;
	}
	.meta {
		margin-left: 3px;
		color: var(--rb-text-dim, #838990);
		overflow: hidden;
		text-overflow: ellipsis;
		min-width: 0;
	}
</style>
