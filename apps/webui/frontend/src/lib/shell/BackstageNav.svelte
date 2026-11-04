<script lang="ts">
	/**
	 * Backstage sidebar (shell.backstage flag ON only; see backstage-nav.ts).
	 * Brand: the Open DJ mark closed into a whole circle (favicon.svg halves
	 * translated together) beside the Anybody 800 / 150% wordmark.
	 * Keyboard: arrows and W/A/S/D move focus among non-inert links, with a
	 * modifier jumping to the ends; a focused text field always wins. Nothing
	 * here touches audio, so a playing deck is never interrupted.
	 */
	import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
	import {
		backstageNav,
		isCurrentBackstageItem,
		navArrowKey,
		nextNavIndex,
		type BackstageItem
	} from './backstage-nav';

	interface Props {
		showDev: boolean;
		url: URL;
	}
	const { showDev, url }: Props = $props();
	const nav = $derived(backstageNav(showDev));

	let root: HTMLElement | undefined = $state();

	function onKeydown(event: KeyboardEvent): void {
		if (isTextEntryTarget(event.target)) return;
		const arrow = navArrowKey(event.key);
		if (arrow === null || root === undefined) return;
		const links = Array.from(root.querySelectorAll<HTMLAnchorElement>('a[data-nav-item]'));
		const current = links.indexOf(document.activeElement as HTMLAnchorElement);
		const modified = event.shiftKey || event.ctrlKey || event.altKey || event.metaKey;
		const next = nextNavIndex(current, links.length, arrow, modified);
		if (next < 0) return;
		event.preventDefault();
		links[next].focus();
	}
</script>

{#snippet item(it: BackstageItem)}
	{#if it.href === null}
		<span
			class="bs-item inert"
			class:soon={it.inert === 'soon'}
			aria-disabled="true"
			title={it.title}
			data-inert={it.inert}>{it.label}</span
		>
	{:else}
		{@const current = isCurrentBackstageItem(it, url)}
		<a
			href={it.href}
			class="bs-item"
			class:active={current}
			aria-current={current ? 'page' : undefined}
			title={it.title}
			data-nav-item={it.id}>{it.label}</a
		>
	{/if}
{/snippet}

<div class="brand" data-testid="backstage-brand">
	<svg class="mark" viewBox="80 80 352 352" aria-hidden="true"
		><path d="M 367.72,144.28 A 188.00,188.00 0 0,0 101.85,410.15 Z" fill="#9c4b34" transform="translate(21.21,-21.21)" /><path
			d="M 410.15,101.85 A 188.00,188.00 0 0,1 144.28,367.72 Z"
			fill="#D97757"
			transform="translate(-21.21,21.21)"
		/></svg
	>
	<span>Open DJ</span>
</div>
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<nav bind:this={root} onkeydown={onKeydown} aria-label="Backstage" data-testid="backstage-nav">
	<div class="gig">{@render item(nav.gig)}</div>
	<div class="bs-label">Backstage</div>
	{#each nav.backstage as group (group.id)}
		<div class="group" data-group={group.id}>
			<div class="group-label">{group.label}</div>
			{#each group.items as it (it.id)}{@render item(it)}{/each}
		</div>
	{/each}
	{#if nav.developer.length > 0}
		<div class="bs-label dev" role="separator">Developer</div>
		{#each nav.developer as group (group.id)}
			<div class="group" data-group={group.id}>
				<div class="group-label">{group.label}</div>
				{#each group.items as it (it.id)}{@render item(it)}{/each}
			</div>
		{/each}
	{/if}
</nav>

<style>
	.brand {
		font-family: var(--rb-font-brand);
		font-weight: 800;
		font-stretch: 150%;
		font-size: 17px;
		letter-spacing: -0.01em;
		color: var(--fg);
		padding: 0 8px 12px;
		display: flex;
		align-items: center;
		gap: 9px;
	}
	.mark {
		width: 24px;
		height: 24px;
		flex: none;
	}
	nav {
		display: flex;
		flex-direction: column;
	}
	.bs-label {
		font-family: var(--rb-font-brand);
		font-weight: 800;
		font-stretch: 125%;
		font-size: 12px;
		letter-spacing: 0.04em;
		color: var(--muted);
		padding: 10px 8px 2px;
		text-transform: uppercase;
	}
	.bs-label.dev {
		border-top: 1px solid var(--border, #1c222c);
		margin-top: 8px;
	}
	.group-label {
		font-size: 0.72rem;
		color: var(--muted);
		padding: 6px 8px 2px;
	}
	.bs-item {
		display: block;
		padding: 3px 8px 3px 14px;
		color: var(--fg);
		text-decoration: none;
		border-radius: 4px;
	}
	.gig .bs-item {
		padding-left: 8px;
		font-weight: 600;
	}
	a.bs-item:hover,
	a.bs-item:focus-visible {
		background: var(--chip-bg);
	}
	a.bs-item[aria-current='page'] {
		color: var(--accent);
		background: var(--chip-bg);
	}
	.inert {
		color: var(--muted);
		cursor: default;
	}
	.soon {
		opacity: 0.45;
	}
</style>
