<script lang="ts">
	/**
	 * The edit modal's settings grid (spec 7.4, mock `openSlotPop` `.grid2`), in the deck's own
	 * words: Launch & sync to, Key, Tempo, Mode, the Downbeat nudge and Level. Every control
	 * carries a `data-tip` for the modal's explainer row. Each change saves the full settings
	 * through the store's `updateSlot` (rolled back with a toast on failure); Level saves on release.
	 */
	import {
		DOWNBEAT_LIMIT,
		SETTING_ROWS,
		downbeatLabel,
		settingsWith
	} from '$lib/player/sampler/sample-edit';
	import { updateSlot } from '$lib/player/sampler/sampler-store.svelte';
	import type { SamplerSlot, SlotSettings } from '$lib/player/sampler/sampler-types';
	import { PLAY_TRIANGLE_PATH } from '$lib/ui/icon-glyphs';

	let { slot }: { slot: SamplerSlot } = $props();

	const DOWNBEAT_TIP =
		'Moves which beat of the sample counts as beat 1, in whole beats. The beatgrid itself is never changed.';
	const NUDGE_TIP = 'Moves beat 1 one beat earlier or later.';
	const LEVEL_TIP = "The sample's volume on the master output.";
	const offset = $derived(slot.settings.downbeat_offset);

	function save<K extends keyof SlotSettings>(key: K, value: SlotSettings[K]): void {
		const settings = settingsWith(slot.settings, key, value);
		// updateSlot toasts and rolls back on failure; the catch stops a second, unhandled report.
		void updateSlot(slot.slot_id, { settings }).catch(() => undefined);
	}
</script>

<div class="grid2">
	{#each SETTING_ROWS as row (row.key)}
		<div>
			<div class="lbl" data-tip={row.tip}>{row.label}</div>
			<div class="seg">
				{#each row.options as o (o.value)}
					<button
						type="button"
						aria-pressed={slot.settings[row.key] === o.value}
						data-tip={o.tip}
						title={o.tip}
						onclick={() => save(row.key, o.value as never)}>{o.text}</button
					>
				{/each}
			</div>
		</div>
	{/each}
	<div class="row">
		<span class="lbl" data-tip={DOWNBEAT_TIP}>Downbeat</span>
		<button
			type="button"
			class="arrow"
			aria-label="One beat earlier"
			data-tip={NUDGE_TIP}
			disabled={offset <= -DOWNBEAT_LIMIT}
			onclick={() => save('downbeat_offset', offset - 1)}
			><svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true"
				><path d="M12 3 L12 13 L4 8 Z" fill="currentColor" /></svg
			></button
		>
		<span class="sum">{downbeatLabel(offset)}</span>
		<button
			type="button"
			class="arrow"
			aria-label="One beat later"
			data-tip={NUDGE_TIP}
			disabled={offset >= DOWNBEAT_LIMIT}
			onclick={() => save('downbeat_offset', offset + 1)}
			><svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true"
				><path d={PLAY_TRIANGLE_PATH} fill="currentColor" /></svg
			></button
		>
	</div>
	<div class="row">
		<span class="lbl" data-tip={LEVEL_TIP}>Level</span>
		<input
			type="range"
			min="0"
			max="100"
			aria-label="Level"
			data-tip={LEVEL_TIP}
			value={slot.settings.level}
			onchange={(ev) => save('level', Number(ev.currentTarget.value))}
		/>
	</div>
</div>

<style>
	.grid2 {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 10px 14px;
	}
	.lbl {
		font: 13px var(--es-font-display);
		text-transform: uppercase;
		letter-spacing: 0.6px;
		color: var(--es-muted);
	}
	.seg {
		display: flex;
		flex-wrap: wrap;
		gap: 5px;
	}
	.seg button {
		font: 13px var(--es-font-body);
		background: var(--es-raised);
		color: var(--es-fg);
		border: 1px solid var(--es-line);
		border-radius: 4px;
		padding: 3px 9px;
		cursor: pointer;
	}
	.seg button[aria-pressed='true'] {
		background: var(--es-accent);
		color: var(--es-accent-ink);
		border-color: var(--es-accent);
	}
	.row {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
	}
	.arrow {
		background: var(--es-raised);
		color: var(--es-fg);
		border: 1px solid var(--es-line);
		border-radius: 4px;
		padding: 0 8px;
		cursor: pointer;
	}
	.arrow:disabled {
		color: var(--es-dim);
		cursor: not-allowed;
	}
	.sum {
		font: 12px var(--es-font-mono);
		color: var(--es-muted);
	}
	input[type='range'] {
		flex: 1;
		accent-color: var(--es-accent);
	}
</style>
