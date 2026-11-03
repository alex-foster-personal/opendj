<script lang="ts">
	/**
	 * Props-only half of PreflightScreen (PREFLIGHT-01): the boot gate and the
	 * admin reference. The container polls the checks, owns every action and
	 * passes the check rows (PreflightCheckRow is itself a container) in as a
	 * snippet; this file draws the brand header, the headline, the errors and
	 * the action bar. Boot mode still has no skip control: the only way past
	 * is a real pass, which the container's parent acts on.
	 */
	import type { Snippet } from 'svelte';
	import AlphaBadge from '$lib/components/AlphaBadge.svelte';
	import { OVERLAY_Z } from '$lib/overlays/stack';
	import type { PreflightActionId, PreflightExplainer } from '$lib/preflight/preflight-actions';
	import PreflightActions from './PreflightActions.svelte';

	let {
		mode = 'boot',
		blocking = true,
		showSurface,
		status,
		bootHeadline,
		engineError,
		firstRunHasError,
		firstRunError,
		visibleActions,
		disabledActions,
		pendingAction,
		actionStatus,
		footer,
		explainers,
		onAction,
		checkRows
	}: {
		mode?: 'boot' | 'admin';
		blocking?: boolean;
		showSurface: boolean;
		/** preflightGate.status, for the data attribute agents and e2e read. */
		status: string;
		bootHeadline: string;
		/** Why the engine could not be reached, or null. */
		engineError: string | null;
		firstRunHasError: boolean;
		firstRunError: string | null;
		visibleActions: PreflightActionId[];
		disabledActions: PreflightActionId[];
		pendingAction: PreflightActionId | null;
		actionStatus: string | null;
		footer: string | null;
		explainers: PreflightExplainer[];
		onAction: (id: PreflightActionId) => void;
		/** The <li> rows, one PreflightCheckRow per visible check. */
		checkRows: Snippet;
	} = $props();
</script>

{#if showSurface}
<section
	class="preflight"
	class:preflight-boot={mode === 'boot' && blocking}
	class:preflight-boot-strip={mode === 'boot' && !blocking}
	class:preflight-admin={mode === 'admin'}
	data-preflight-mode={mode}
	data-preflight-status={status}
	data-preflight-blocking={blocking ? 'true' : 'false'}
	style:--preflight-boot-z={mode === 'boot' && blocking ? OVERLAY_Z.preflightBoot : undefined}
>
	{#if mode === 'boot' && blocking}
		<header class="preflight-brand" aria-label="Open DJ">
			<div class="preflight-mark" aria-hidden="true"></div>
			<div class="preflight-lockup">
				<span class="preflight-name">Open DJ<AlphaBadge /></span>
				<p class="preflight-welcome">
					Welcome. Import your music to start DJing, or keep going once setup finishes.
				</p>
			</div>
		</header>
	{/if}
	<h2>{bootHeadline}</h2>
	{#if engineError}
		<p class="preflight-error">Could not reach the engine: {engineError}</p>
	{/if}
	{#if mode === 'boot' && blocking && firstRunHasError}
		<p class="preflight-error" role="alert">{firstRunError}</p>
	{/if}
	<ul class="preflight-checks">
		{@render checkRows()}
	</ul>
	<PreflightActions
		actions={visibleActions}
		disabled={disabledActions}
		pending={pendingAction}
		status={actionStatus}
		{footer}
		{explainers}
		{onAction}
	/>
</section>
{/if}

<style>
	/* No success/positive token exists in app.css yet; scoped here rather
	   than editing that shared file mid-fan-out (same call admin/+page.svelte
	   already made for --kpi-ok). */
	:global(:root) {
		--preflight-ok: #4ecb8c;
		/* Orange is the "worth saying, not worth stopping for" colour. It is
		   defined next to the green for the same reason that one was scoped
		   here: app.css has no severity tokens yet. */
		--preflight-warn: #e8a33d;
	}
	:global(html:is([data-theme='light'], [data-skin='light'])) {
		--preflight-ok: #2e9e63;
		--preflight-warn: #b3701a;
	}

	.preflight {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		padding: 1rem;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface);
		color: var(--fg);
	}
	.preflight-brand {
		display: flex;
		align-items: center;
		gap: 0.85rem;
	}
	.preflight-mark {
		width: 3rem;
		height: 3rem;
		flex: none;
		background: url('/favicon.svg') center / contain no-repeat;
	}
	.preflight-name {
		font-family: var(--rb-font-brand);
		display: block;
		font-size: 1.15rem;
		font-weight: 700;
		letter-spacing: -0.01em;
	}
	.preflight-welcome {
		margin: 0.2rem 0 0;
		color: var(--muted);
		font-size: 0.9em;
	}
	.preflight-boot {
		position: fixed;
		inset: 0;
		z-index: var(--preflight-boot-z, 360);
		max-width: 520px;
		margin: 10vh auto;
		height: fit-content;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
		background: var(--surface);
	}
	.preflight-boot::before {
		content: '';
		position: fixed;
		inset: 0;
		z-index: -1;
		background: rgba(0, 0, 0, 0.55);
		pointer-events: none;
	}
	.preflight-boot-strip {
		position: fixed;
		top: 0;
		left: 0;
		right: 0;
		z-index: 360;
		max-width: none;
		margin: 0;
		border-radius: 0;
		border-top: none;
		border-left: none;
		border-right: none;
		box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
	}
	.preflight-boot-strip h2 {
		font-size: 0.95rem;
	}
	.preflight-boot-strip .preflight-checks {
		flex-direction: row;
		flex-wrap: wrap;
		gap: 0.75rem 1.25rem;
	}
	.preflight-boot-strip :global(.note) {
		display: none;
	}
	.preflight-error {
		color: var(--danger);
	}
	.preflight-checks {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
</style>
