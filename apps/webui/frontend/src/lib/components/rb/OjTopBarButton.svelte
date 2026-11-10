<script lang="ts">
	/**
	 * OJ-9477: the top bar's OJ button. Toggles the agent sidebar (AgentSidebarMount)
	 * through the `oj_toggle` performance command, so a click and an agent take one
	 * path. Renders nothing unless the `assistant.sidebar` flag is on and
	 * GET /api/v1/assistant/status says configured. The glyph is a placeholder
	 * glass; swap the <svg> for the OJ character art when it lands.
	 */
	import { assistantSidebarEnabled } from '$lib/assistant/sidebar-flag';
	import { createAssistantStatusProbe } from '$lib/assistant/status-probe.svelte';
	import { ojSidebar } from '$lib/assistant/oj-sidebar.svelte';
	import { ojTopBarButtonVisible } from '$lib/assistant/oj-sidebar-toggle';

	let {
		ontoggle,
		isEnabled = assistantSidebarEnabled
	}: { ontoggle: () => void; isEnabled?: () => boolean } = $props();

	const enabled = $derived(isEnabled());
	const probe = createAssistantStatusProbe(() => enabled);
	const visible = $derived(ojTopBarButtonVisible(enabled, probe.status, probe.statusError));
</script>

{#if visible}
	<button
		type="button"
		class="oj-topbar-button"
		data-testid="oj-topbar-button"
		aria-label={ojSidebar.open ? 'Close OJ' : 'Open OJ'}
		aria-pressed={ojSidebar.open}
		title={ojSidebar.open ? 'Close OJ' : 'Open OJ'}
		onclick={ontoggle}
	>
		<svg viewBox="0 0 16 16" aria-hidden="true">
			<path d="M4 3h8l-1 9a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1z" />
			<path d="M4.6 7h6.8" />
		</svg>
	</button>
{/if}

<style>
	.oj-topbar-button {
		display: inline-grid;
		place-items: center;
		width: 1.6rem;
		height: 1.6rem;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface-raised);
		color: var(--muted);
		cursor: pointer;
		flex: none;
	}
	.oj-topbar-button:hover,
	.oj-topbar-button:focus-visible,
	.oj-topbar-button[aria-pressed='true'] {
		color: var(--fg);
	}
	.oj-topbar-button svg {
		width: 0.9rem;
		height: 0.9rem;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.4;
		stroke-linejoin: round;
		stroke-linecap: round;
	}
</style>
