<script lang="ts">
	import type { SettingDef } from '$lib/settings/catalog';
	import { effectiveKeymap, getKeymapPreset } from '$lib/keyboard/keymap';
	import { unboundCommands } from '$lib/keyboard/presets/index';
	import { recordKeymapFromKeydown } from '$lib/keyboard/keymap-record';
	import { setKeyboardOverrides, uiPrefs } from '$lib/rb/prefs.svelte';

	let { def: _def }: { def: SettingDef } = $props();

	let recordingId = $state<string | null>(null);
	let conflictLabel = $state<string | null>(null);

	const presetKeymap = $derived(getKeymapPreset(uiPrefs.keyboard.preset));
	const effective = $derived(effectiveKeymap(presetKeymap, uiPrefs.keyboard.overrides));
	const unbound = $derived(unboundCommands(effective));
	const groups = $derived(
		[...new Set(unbound.map((cmd) => cmd.group))].sort((a, b) => a.localeCompare(b))
	);

	function startRecord(commandId: string): void {
		recordingId = commandId;
		conflictLabel = null;
	}

	function cancelRecord(): void {
		recordingId = null;
		conflictLabel = null;
	}

	function onCaptureKeydown(e: KeyboardEvent): void {
		if (recordingId === null) return;
		const outcome = recordKeymapFromKeydown(
			presetKeymap,
			uiPrefs.keyboard.overrides,
			recordingId,
			e,
			globalThis.navigator?.platform ?? ''
		);
		if (outcome.kind === 'cancelled') {
			cancelRecord();
			return;
		}
		if (outcome.kind === 'ignored') return;
		if (outcome.kind === 'conflict') {
			conflictLabel = outcome.label;
			return;
		}
		e.preventDefault();
		e.stopPropagation();
		setKeyboardOverrides({
			...uiPrefs.keyboard.overrides,
			[outcome.commandId]: [outcome.chord]
		});
		cancelRecord();
	}
</script>

<svelte:window onkeydowncapture={onCaptureKeydown} />

<section class="kb-unbound" aria-labelledby="kb-unbound-heading">
	<h3 id="kb-unbound-heading">Not bound in this layout</h3>
	{#if conflictLabel !== null}
		<p class="kb-unbound-conflict" role="status">Already bound to {conflictLabel}</p>
	{/if}
	{#each groups as group (group)}
		<details class="kb-unbound-group" open={group !== 'Deck 3' && group !== 'Deck 4'}>
			<summary>{group}</summary>
			<ul>
				{#each unbound.filter((cmd) => cmd.group === group) as cmd (cmd.id)}
					<li class="kb-unbound-row">
						<span>{cmd.label}</span>
						<button
							type="button"
							aria-pressed={recordingId === cmd.id}
							onclick={() => (recordingId === cmd.id ? cancelRecord() : startRecord(cmd.id))}
						>
							{recordingId === cmd.id ? 'Press a key…' : 'Record'}
						</button>
					</li>
				{/each}
			</ul>
		</details>
	{/each}
</section>

<style>
	.kb-unbound {
		margin-top: 0.75rem;
	}
	.kb-unbound h3 {
		margin: 0 0 0.5rem;
		font-size: 0.95rem;
	}
	.kb-unbound-conflict {
		color: var(--color-warn, #e67e22);
		font-size: 0.85rem;
	}
	.kb-unbound-group {
		margin-bottom: 0.35rem;
	}
	.kb-unbound-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.5rem;
		padding: 0.15rem 0;
	}
</style>
