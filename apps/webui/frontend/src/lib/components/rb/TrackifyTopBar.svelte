<script lang="ts">
	import { page } from '$app/stores';
	import { APP_MODES } from '$lib/rb/app-mode';
	import { modeIconClass } from '$lib/rb/app-mode-icons';
	import { appModeForPath } from '$lib/rb/app-mode';
	import { setAppMode } from '$lib/rb/prefs.svelte';

	let modePickerEl: HTMLDetailsElement | undefined = $state();
	let modeMenuEl: HTMLDivElement | undefined = $state();

	const activeMode = $derived(appModeForPath($page.url.pathname));

	function _selectAppMode(id: typeof activeMode.id): void {
		setAppMode(id);
	}

	function _dismissModeMenuOnOutsidePointer(event: PointerEvent): void {
		if (modePickerEl === undefined || !modePickerEl.open) return;
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (modePickerEl.contains(target) || modeMenuEl?.contains(target)) return;
		modePickerEl.open = false;
	}

	function _dismissModeMenuOnEscape(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || modePickerEl === undefined) return;
		modePickerEl.open = false;
	}
</script>

<svelte:window onpointerdown={_dismissModeMenuOnOutsidePointer} onkeydown={_dismissModeMenuOnEscape} />

<header class="trackify-topbar">
	<details class="mode-picker" bind:this={modePickerEl}>
		<summary class="mode-dd" aria-label="Choose app mode">
			{activeMode.label.toUpperCase()}
		</summary>
		<div class="mode-menu" bind:this={modeMenuEl} aria-label="App modes">
			<p class="mode-menu-heading">Choose app mode</p>
			{#each APP_MODES as mode (mode.id)}
				<a
					class="mode-card"
					data-testid="mode-card"
					href={mode.href}
					aria-current={mode.id === activeMode.id ? 'page' : undefined}
					onclick={() => _selectAppMode(mode.id)}
				>
					<span class={`mode-thumbnail ${modeIconClass(mode.iconId)}`} aria-hidden="true"></span>
					<span class="mode-copy">
						<strong>{mode.label}</strong>
						<span class="mode-gain" data-testid="mode-gain">{mode.gain}</span>
						<span class="mode-lose" data-testid="mode-lose">{mode.lose}</span>
					</span>
				</a>
			{/each}
		</div>
	</details>
</header>

<style>
	.trackify-topbar {
		display: flex;
		align-items: center;
		padding: 0.75rem 1rem;
		border-bottom: 1px solid color-mix(in srgb, var(--text) 12%, transparent);
	}
	.mode-picker {
		position: relative;
	}
	.mode-dd {
		cursor: pointer;
		list-style: none;
		font-weight: 600;
	}
	.mode-menu {
		position: absolute;
		top: calc(100% + 0.25rem);
		left: 0;
		z-index: 20;
		min-width: 18rem;
		padding: 0.5rem;
		border-radius: 0.5rem;
		background: var(--panel, #111);
		border: 1px solid color-mix(in srgb, var(--text) 16%, transparent);
	}
	.mode-card {
		display: flex;
		gap: 0.75rem;
		padding: 0.5rem;
		text-decoration: none;
		color: inherit;
	}
	.mode-thumbnail {
		width: 2rem;
		height: 2rem;
		border-radius: 0.35rem;
		background: color-mix(in srgb, var(--text) 12%, transparent);
	}
	.mode-thumbnail.trackify {
		background: linear-gradient(135deg, #5b8cff, #9b6bff);
	}
	.mode-copy {
		display: grid;
		gap: 0.15rem;
	}
	.mode-gain,
	.mode-lose {
		font-size: 0.8rem;
		opacity: 0.85;
	}
</style>
