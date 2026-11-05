<script lang="ts">
	/**
	 * WAVEQS-8348b..8348d (issue #8343): the waveform quick-settings popover. Loaded
	 * lazily on the top-bar glyph's first click (bundle bucket
	 * `wave-quick-settings`), so it costs first paint nothing.
	 *
	 * A second VIEW over the Settings catalog, never a second copy: each live
	 * row renders its catalog entry's label, explanation and preview, reads
	 * through `readSettingValue` and writes through `applySettingChange`, the
	 * exact path the Settings overlay and the AI apply use. So a change here
	 * shows in Settings at once and the reverse, and persists through the same
	 * ui-prefs PUT.
	 */
	import { onMount, tick } from 'svelte';
	import SettingPreview from '$lib/components/settings/SettingPreview.svelte';
	import { SETTINGS_CATALOG } from '$lib/settings/catalog';
	import type { SettingDef } from '$lib/settings/catalog-types';
	import { applySettingChange, isAllowedSettingKey, readSettingValue } from '$lib/settings/apply';
	import { openSettings } from '$lib/settings/hotkeys';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import {
		WAVE_QS_GROUPS,
		WAVE_QS_INERT_TITLE,
		resolveWaveQsSetting,
		waveQsRowTitle,
		type WaveQsRow
	} from '$lib/settings/waveform-quick-settings';

	let { anchor, onclose }: { anchor: HTMLElement; onclose: () => void } = $props();

	let panelEl: HTMLDivElement | undefined = $state();
	let panelStyle = $state('visibility:hidden');

	/** Resolved once: a row naming a missing or unimplemented entry throws here, loudly. */
	const groups = WAVE_QS_GROUPS.map((group) => ({
		...group,
		rows: group.rows.map((row) => ({ row, def: _defFor(row) }))
	}));

	function _defFor(row: WaveQsRow): SettingDef | null {
		if (row.kind === 'inert') return null;
		const def = resolveWaveQsSetting(SETTINGS_CATALOG, row.id);
		if (!isAllowedSettingKey(def.id))
			throw new Error(`waveform quick settings: ${def.id} is not an allowed setting key`);
		return def;
	}

	function _value(def: SettingDef): boolean | string {
		if (!isAllowedSettingKey(def.id))
			throw new Error(`waveform quick settings: ${def.id} is not an allowed setting key`);
		return readSettingValue(def.id);
	}

	function _place(): void {
		const panel = panelEl;
		if (panel === undefined) return;
		const r = anchor.getBoundingClientRect();
		const box = clampToViewport(
			r.right - panel.offsetWidth,
			r.bottom + 6,
			{ width: panel.offsetWidth, height: panel.offsetHeight },
			{ width: window.innerWidth, height: window.innerHeight }
		);
		panelStyle = `left:${Math.round(box.x)}px;top:${Math.round(box.y)}px`;
	}

	function _openFullSettings(): void {
		onclose();
		openSettings();
	}

	onMount(() => {
		// Place first, then focus after the placed style lands: a panel still at
		// visibility:hidden cannot take focus. Focus goes to the first OPTION, not
		// the header's All settings button. Closed before the ticks ran: nothing to do.
		let mounted = true;
		void tick()
			.then(() => {
				if (mounted) _place();
				return tick();
			})
			.then(() => {
				if (!mounted) return;
				const first = panelEl?.querySelector<HTMLElement>(
					'.wqs-group select, .wqs-group input:not(:disabled)'
				);
				if (first === undefined || first === null)
					throw new Error('waveform quick settings: no option control to focus');
				first.focus();
			});
		const onPointer = (e: PointerEvent): void => {
			const target = e.target;
			if (target instanceof Node && (panelEl?.contains(target) || anchor.contains(target))) return;
			onclose();
		};
		const onKey = (e: KeyboardEvent): void => {
			if (e.key !== 'Escape') return;
			e.preventDefault();
			e.stopPropagation();
			onclose();
			anchor.focus();
		};
		window.addEventListener('pointerdown', onPointer, true);
		window.addEventListener('keydown', onKey, true);
		window.addEventListener('resize', _place);
		return () => {
			mounted = false;
			window.removeEventListener('pointerdown', onPointer, true);
			window.removeEventListener('keydown', onKey, true);
			window.removeEventListener('resize', _place);
		};
	});
</script>

<div
	bind:this={panelEl}
	id="wave-quick-settings"
	class="wqs"
	role="dialog"
	aria-label="Waveform quick settings"
	style={panelStyle}
>
	<header class="wqs-head">
		<span class="wqs-title">Waveform</span>
		<button
			type="button"
			class="wqs-all"
			title="Open every setting in the Settings overlay (Cmd+,)"
			onclick={_openFullSettings}
		>
			All settings
		</button>
	</header>
	{#each groups as group (group.id)}
		<section class="wqs-group" aria-label={group.label}>
			<h3 class="wqs-group-label">{group.label}</h3>
			<ul>
				{#each group.rows as { row, def } (row.id)}
					{#if def === null && row.kind === 'inert'}
						<li class="wqs-row inert" data-wqs-row={row.id} title={WAVE_QS_INERT_TITLE}>
							<span class="wqs-label">{row.label}</span>
							<input
								type="checkbox"
								checked
								disabled
								aria-label={row.label}
								title={WAVE_QS_INERT_TITLE}
							/>
							<p class="wqs-detail">{row.detail}</p>
						</li>
					{:else if def !== null}
						<li class="wqs-row" data-wqs-row={def.id}>
							<label class="wqs-label" for={`wqs-${def.id}`} title={waveQsRowTitle(def)}
								>{def.label}</label
							>
							{#if def.control.kind === 'boolean'}
								<input
									id={`wqs-${def.id}`}
									type="checkbox"
									role="switch"
									checked={_value(def) === true}
									title={`${def.label}: ${_value(def) === true ? 'on' : 'off'}. ${def.title}`}
									onchange={() => applySettingChange(def.id, _value(def) !== true)}
								/>
							{:else if def.control.kind === 'enum'}
								<select
									id={`wqs-${def.id}`}
									value={String(_value(def))}
									title={def.title}
									onchange={(e) =>
										applySettingChange(def.id, (e.currentTarget as HTMLSelectElement).value)}
								>
									{#each def.control.options as opt (opt.value)}
										<option value={opt.value}>{opt.label}</option>
									{/each}
								</select>
							{/if}
							{#if def.preview !== undefined}
								<div class="wqs-preview">
									<SettingPreview kind={def.preview} label={`${def.label} preview`} />
								</div>
							{/if}
							<p class="wqs-detail" title={def.detail}>{def.detail}</p>
						</li>
					{/if}
				{/each}
			</ul>
		</section>
	{/each}
</div>

<style>
	.wqs {
		position: fixed;
		z-index: 380;
		/* Layout C (two columns, one-line explanations; the full text is the hover title):
		   every option fits with no scrolling at 1440x900. Narrow windows fall back to one column. */
		width: min(620px, calc(100vw - 16px));
		columns: 2 280px;
		column-gap: 16px;
		max-height: min(86vh, 760px);
		overflow-y: auto;
		padding: 8px 10px 10px;
		background: var(--rb-panel, #14181f);
		border: 1px solid var(--rb-border, #2a2f38);
		border-radius: 4px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
		color: var(--rb-text, #e6e9ef);
		font-family: var(--rb-font);
		font-size: 11px;
	}
	.wqs-head {
		column-span: all;
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 4px;
	}
	.wqs-title {
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}
	.wqs-all {
		background: transparent;
		border: 1px solid var(--rb-border, #2a2f38);
		border-radius: 2px;
		color: var(--rb-text-dim, #9aa3b2);
		font: inherit;
		font-size: 10px;
		padding: 2px 6px;
		cursor: pointer;
	}
	.wqs-all:hover,
	.wqs-all:focus-visible {
		color: var(--rb-text, #e6e9ef);
	}
	.wqs-group {
		break-inside: avoid;
		border-top: 1px solid var(--rb-border, #2a2f38);
		padding-top: 6px;
		margin-bottom: 6px;
	}
	.wqs-group-label {
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
	.wqs-row {
		display: grid;
		grid-template-columns: 1fr auto;
		align-items: center;
		column-gap: 8px;
		padding: 4px 0;
	}
	.wqs-row.inert {
		opacity: 0.5;
	}
	.wqs-label {
		cursor: default;
	}
	.wqs-row select {
		max-width: 150px;
		font: inherit;
		font-size: 10px;
		background: var(--rb-surface, #0d1016);
		color: var(--rb-text, #e6e9ef);
		border: 1px solid var(--rb-border, #2a2f38);
		border-radius: 2px;
		padding: 1px 2px;
	}
	.wqs-row input[type='checkbox'] {
		accent-color: var(--rb-accent);
		margin: 0;
	}
	.wqs-preview {
		grid-column: 1 / -1;
	}
	.wqs-detail {
		grid-column: 1 / -1;
		margin: 2px 0 0;
		font-size: 10px;
		line-height: 1.35;
		color: var(--rb-text-dim, #9aa3b2);
		display: -webkit-box;
		-webkit-line-clamp: 1;
		line-clamp: 1;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.wqs :focus-visible {
		outline: 1px solid var(--rb-accent);
		outline-offset: 1px;
	}
</style>
