<script lang="ts">
	/**
	 * WAVEQS-8348 (issue #8343): the top bar's waveform glyph (2:1, "2 wide 1
	 * deep"). Click toggles the quick-settings popover; the popover module is
	 * imported on the first click only, so it stays out of first paint.
	 * Hidden unless the daemon declares the `waveform.quick_settings` flag on.
	 */
	import { waveQsEnabled } from '$lib/rb/waveform-quick-settings-flag';
	import { topbarSelectorNode } from './topbar-selector-node';

	type PopoverModule = typeof import('./WaveQuickSettingsPopover.svelte');

	let buttonEl: HTMLButtonElement | undefined = $state();
	let open = $state(false);
	let Popover = $state<PopoverModule['default'] | null>(null);
	const enabled = $derived(waveQsEnabled());

	/** WAVEQS-8348: the hover title, verbatim. A load failure never replaces it; it
	 * rethrows to the app's error reporter instead. */
	const TITLE = 'Waveform quick settings';

	/** The click's intent is recorded at once, so a second click while the
	 * chunk is still loading closes it rather than racing the import. */
	function _toggle(): void {
		open = !open;
		if (!open || Popover !== null) return;
		import('./WaveQuickSettingsPopover.svelte')
			.then((mod) => {
				Popover = mod.default;
			})
			.catch((err: unknown) => {
				open = false;
				throw new Error(`Waveform quick settings failed to load: ${String(err)}`, { cause: err });
			});
	}
</script>

{#if enabled}
	<button
		bind:this={buttonEl}
		type="button"
		class="tb-icon theme-toggle topbar-slot-pinned wqs-glyph"
		class:on={open}
		title={TITLE}
		aria-label="Waveform quick settings"
		aria-haspopup="dialog"
		aria-expanded={open}
		aria-controls={open ? 'wave-quick-settings' : undefined}
		data-testid="wave-quick-settings-button"
		use:topbarSelectorNode={{ name: 'wave-settings', disabled: false }}
		onclick={_toggle}
	>
		<svg width="16" height="8" viewBox="0 0 16 8" aria-hidden="true">
			<path
				d="M1 3.2v1.6M3 2v4M5 0.8v6.4M7 2.6v2.8M9 1.4v5.2M11 0.4v7.2M13 2.2v3.6M15 3v2"
				stroke="currentColor"
				stroke-width="1.2"
				stroke-linecap="round"
			/>
		</svg>
	</button>
	{#if open && Popover !== null && buttonEl !== undefined}
		<Popover anchor={buttonEl} onclose={() => (open = false)} />
	{/if}
{/if}

<style>
	.wqs-glyph {
		padding: 2px 4px;
	}
</style>
