<script lang="ts">
	/**
	 * CUSTSCR-9441 (issue #9441): inline catalogue-backed customise screen. A second view
	 * over `SETTINGS_CATALOG`: each live row renders that entry's label, explanation and
	 * preview, reads through `readSettingValue` and writes through `applySettingChange`.
	 *
	 * Deck rows have no command-bus verb; labels use `def.title` as the hover title (never the
	 * waveform quick-settings helper that requires a CLI verb per row). Number rows render as a
	 * stepper (CUSTSCR-10464, issue #10202).
	 */
	import SettingNumberStepper from '$lib/components/settings/SettingNumberStepper.svelte';
	import SettingPreview from '$lib/components/settings/SettingPreview.svelte';
	import { SETTINGS_CATALOG } from '$lib/settings/catalog';
	import type { SettingDef } from '$lib/settings/catalog-types';
	import { applySettingChange, isAllowedSettingKey, readSettingValue } from '$lib/settings/apply';
	import {
		resolveCustomiseSetting,
		type CustomiseRow,
		type CustomiseScreenDef
	} from '$lib/settings/customise-screens';
	import { WAVE_QS_INERT_TITLE } from '$lib/settings/waveform-quick-settings';

	let { def }: { def: CustomiseScreenDef } = $props();

	const groups = $derived.by(() => {
		function _resolved(row: CustomiseRow): SettingDef | null {
			if (row.kind === 'inert') return null;
			const setting = resolveCustomiseSetting(SETTINGS_CATALOG, def.id, row.id);
			if (!isAllowedSettingKey(setting.id))
				throw new Error(`customise screen ${def.id}: ${setting.id} is not an allowed setting key`);
			return setting;
		}
		return def.groups.map((group) => ({
			...group,
			rows: group.rows.map((row) => ({ row, resolved: _resolved(row) }))
		}));
	});

	function _value(entry: SettingDef): boolean | string {
		if (!isAllowedSettingKey(entry.id))
			throw new Error(`customise screen ${def.id}: ${entry.id} is not an allowed setting key`);
		return readSettingValue(entry.id);
	}

	function _number(entry: SettingDef): number {
		const raw = _value(entry);
		const parsed = typeof raw === 'string' ? Number(raw) : NaN;
		if (!Number.isFinite(parsed))
			throw new Error(
				`customise screen ${def.id}: ${entry.id} is a number setting but read back ${JSON.stringify(raw)}`
			);
		return parsed;
	}
</script>

<section class="cs" aria-label={def.label} data-customise-screen={def.id}>
	<h2 class="cs-title">{def.label}</h2>
	{#each groups as group (group.id)}
		<section class="cs-group" aria-label={group.label}>
			<h3 class="cs-group-label">{group.label}</h3>
			<ul>
				{#each group.rows as { row, resolved } (row.id)}
					{#if resolved === null && row.kind === 'inert'}
						<li class="cs-row inert" data-customise-row={row.id} title={WAVE_QS_INERT_TITLE}>
							<span class="cs-label">{row.label}</span>
							<input
								type="checkbox"
								checked
								disabled
								aria-label={row.label}
								title={WAVE_QS_INERT_TITLE}
							/>
							<p class="cs-detail">{row.detail}</p>
						</li>
					{:else if resolved !== null}
						{@const s = resolved}
						<li class="cs-row" data-customise-row={s.id}>
							<label
								class="cs-label"
								for={`customise-${def.id}-${s.id}`}
								title={s.title}>{s.label}</label
							>
							{#if s.control.kind === 'boolean'}
								<input
									id={`customise-${def.id}-${s.id}`}
									type="checkbox"
									role="switch"
									checked={_value(s) === true}
									title={`${s.label}: ${_value(s) === true ? 'on' : 'off'}. ${s.title}`}
									onchange={() => applySettingChange(s.id, _value(s) !== true)}
								/>
							{:else if s.control.kind === 'enum'}
								<select
									id={`customise-${def.id}-${s.id}`}
									value={String(_value(s))}
									title={s.title}
									onchange={(e) =>
										applySettingChange(s.id, (e.currentTarget as HTMLSelectElement).value)}
								>
									{#each s.control.options as opt (opt.value)}
										<option value={opt.value}>{opt.label}</option>
									{/each}
								</select>
							{:else if s.control.kind === 'number'}
								<SettingNumberStepper
									id={`customise-${def.id}-${s.id}`}
									label={s.label}
									title={s.title}
									control={s.control}
									value={_number(s)}
									onchange={(next) => applySettingChange(s.id, String(next))}
								/>
							{/if}
							{#if s.preview !== undefined}
								<div class="cs-preview">
									<SettingPreview kind={s.preview} label={`${s.label} preview`} />
								</div>
							{/if}
							<p class="cs-detail" title={s.detail}>{s.detail}</p>
						</li>
					{/if}
				{/each}
			</ul>
		</section>
	{/each}
</section>

<style>
	.cs {
		padding: 8px 10px 10px;
		background: var(--rb-panel, #14181f);
		border: 1px solid var(--rb-border, #2a2f38);
		border-radius: 4px;
		color: var(--rb-text, #e6e9ef);
		font-family: var(--rb-font);
		font-size: 11px;
		max-width: 620px;
	}
	.cs-title {
		margin: 0 0 8px;
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}
	.cs-group {
		border-top: 1px solid var(--rb-border, #2a2f38);
		padding-top: 6px;
		margin-bottom: 6px;
	}
	.cs-group-label {
		margin: 0 0 4px;
		font-size: 9px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--rb-text-dim, #9aa3b2);
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.cs-row {
		display: grid;
		grid-template-columns: 1fr auto;
		align-items: center;
		column-gap: 8px;
		padding: 4px 0;
	}
	.cs-row.inert {
		opacity: 0.5;
	}
	.cs-label {
		cursor: default;
	}
	.cs-row select {
		max-width: 150px;
		font: inherit;
		font-size: 10px;
		background: var(--rb-surface, #0d1016);
		color: var(--rb-text, #e6e9ef);
		border: 1px solid var(--rb-border, #2a2f38);
		border-radius: 2px;
		padding: 1px 2px;
	}
	.cs-row input[type='checkbox'] {
		accent-color: var(--rb-accent);
		margin: 0;
	}
	.cs-preview {
		grid-column: 1 / -1;
	}
	.cs-detail {
		grid-column: 1 / -1;
		margin: 2px 0 0;
		font-size: 10px;
		line-height: 1.35;
		color: var(--rb-text-dim, #9aa3b2);
	}
	.cs :focus-visible {
		outline: 1px solid var(--rb-accent);
		outline-offset: 1px;
	}
</style>
