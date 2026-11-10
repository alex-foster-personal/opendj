<script lang="ts">
	/**
	 * The assistant's door on every route, /performance and / included: a
	 * right-edge tab and Cmd+I (Ctrl+I off Mac). The sidebar is a lazy chunk
	 * (the `ai-sidebar` bundle budget) fetched on the first open, then kept
	 * mounted and only hidden, so closing it does not lose the conversation.
	 * It overlays the page with fixed positioning and never takes layout room,
	 * so /performance does not reflow when it opens.
	 *
	 * AISIDE-7892j: behind `assistant.sidebar`, default OFF. While it is off there
	 * is no tab and Cmd+I passes through untouched.
	 *
	 * OJ-9477: open state is shared (oj-sidebar.svelte.ts) so the top bar's OJ
	 * button and the `oj_toggle` performance command reach the same panel. It
	 * slides in only when ojMotionPolicy allows it: never while a deck plays on
	 * /performance, never under prefers-reduced-motion.
	 */
	import { untrack } from 'svelte';
	import { anyDeckPlaying } from '$lib/rb/playing-gate';
	import { ojSidebar, setOjSidebarOpen, toggleOjSidebar } from '$lib/assistant/oj-sidebar.svelte';
	import { ojSidebarAnimates } from '$lib/assistant/oj-sidebar-toggle';
	import { assistantSidebarEnabled } from '$lib/assistant/sidebar-flag';
	import { pushToast, TOAST_DEFAULT_MS } from '$lib/stores.svelte';
	import { assistantChordApplies } from '$lib/assistant/sidebar-toggle';
	import {
		ASSISTANT_CHORD_ARIA,
		ASSISTANT_CHORD_LABEL
	} from '$lib/components/rb/hotkeys/hotkeys-registry';

	type SidebarModule = typeof import('./AgentSidebar.svelte');

	/** Other right-edge fixed chrome (the feedback dock) offsets itself by this. */
	const DOCK_WIDTH_VAR = '--assistant-dock-width';

	let { isEnabled = assistantSidebarEnabled }: { isEnabled?: () => boolean } = $props();
	const enabled = $derived(isEnabled());
	const open = $derived(ojSidebar.open);
	let sidebar = $state.raw<Promise<SidebarModule> | null>(null);
	let dockWidth = $state(0);

	$effect(() => {
		const root = document.documentElement.style;
		root.setProperty(DOCK_WIDTH_VAR, `${dockWidth}px`);
		return () => root.removeProperty(DOCK_WIDTH_VAR);
	});

	function toggle(): void {
		toggleOjSidebar();
	}

	function setOpen(next: boolean): void {
		setOjSidebarOpen(next);
	}

	// Any opener (tab, chord, top bar button, oj_toggle) fetches the chunk on first open.
	$effect(() => {
		if (open && sidebar === null) load();
	});

	function onPerformanceRoute(): boolean {
		return typeof location !== 'undefined' && location.pathname.startsWith('/performance');
	}

	function prefersReducedMotion(): boolean {
		return (
			typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
		);
	}

	/** Read once per open, so the decision is the one in force when the panel appears. */
	const animates = $derived(
		open &&
			untrack(() =>
				ojSidebarAnimates({
					anyDeckPlaying: anyDeckPlaying(),
					performanceVisible: onPerformanceRoute(),
					reducedMotion: prefersReducedMotion()
				})
			)
	);

	function load(): void {
		const attempt = import('./AgentSidebar.svelte');
		sidebar = attempt;
		attempt.catch((error: unknown) => {
			if (sidebar === attempt) sidebar = null;
			setOjSidebarOpen(false);
			const message = error instanceof Error ? error.message : String(error);
			pushToast(
				`Assistant failed to load: ${message}. Reload the page to retry.`,
				'error',
				TOAST_DEFAULT_MS,
				error
			);
		});
	}

	function onKey(event: KeyboardEvent): void {
		if (!assistantChordApplies(enabled, event)) return;
		event.preventDefault();
		toggle();
	}
</script>

<svelte:window onkeydown={onKey} />

{#if enabled && !open}
	<button
		type="button"
		class="assistant-tab"
		title="Open the assistant ({ASSISTANT_CHORD_LABEL})"
		aria-label="Open the assistant"
		aria-keyshortcuts={ASSISTANT_CHORD_ARIA}
		onclick={toggle}
	>
		<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4h10v6H8l-3 3v-3H3z" /></svg>
	</button>
{/if}
{#if sidebar}
	<div
		class="assistant-dock"
		class:oj-animate={animates}
		data-oj-sidebar-motion={animates ? 'animated' : 'still'}
		hidden={!open}
		bind:clientWidth={dockWidth}
	>
		{#await sidebar then { default: AgentSidebar }}
			<AgentSidebar {open} onclose={() => setOpen(false)} />
		{:catch}
			<!-- Already reported as an error toast by load(). -->
		{/await}
	</div>
{/if}

<style>
	.assistant-tab {
		position: fixed;
		right: 0;
		top: 45%;
		z-index: 340;
		display: grid;
		place-items: center;
		width: 1.4rem;
		height: 2.6rem;
		padding: 0;
		border: 1px solid var(--border);
		border-right: none;
		border-radius: 6px 0 0 6px;
		background: var(--surface-raised);
		color: var(--muted);
		opacity: 0.75;
		cursor: pointer;
	}
	.assistant-tab:hover,
	.assistant-tab:focus-visible {
		opacity: 1;
		color: var(--fg);
	}
	.assistant-tab svg {
		width: 0.85rem;
		height: 0.85rem;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.5;
		stroke-linejoin: round;
	}
	.assistant-dock {
		position: fixed;
		top: 0;
		right: 0;
		bottom: 0;
		z-index: 340;
		box-shadow: -8px 0 24px rgba(0, 0, 0, 0.35);
	}
	.assistant-dock.oj-animate {
		animation: oj-slide-in 180ms ease-out;
	}
	@keyframes oj-slide-in {
		from {
			transform: translateX(100%);
		}
		to {
			transform: translateX(0);
		}
	}
	.assistant-dock[hidden] {
		display: none;
	}
</style>
