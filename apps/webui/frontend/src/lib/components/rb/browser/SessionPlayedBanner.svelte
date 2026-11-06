<script lang="ts">
	// SESSION-PLAYED-01: an expired session (idle over 12 h) is never dropped
	// silently: the DJ restores it or starts a new one. "New session" stays
	// available while live too, as the one explicit way to clear the green rows.
	import { formatSessionWhen } from '$lib/rb/session-played';
	import { restoreLastSession, sessionPlayed, startNewSession } from '$lib/rb/session-played.svelte';

	let busy = $state(false);
	const run = (fn: () => Promise<void>) => async () => {
		busy = true;
		try {
			await fn();
		} finally {
			busy = false;
		}
	};
	const when = $derived(
		sessionPlayed.doc?.last_activity_ms != null ? formatSessionWhen(sessionPlayed.doc.last_activity_ms) : ''
	);
</script>

{#if sessionPlayed.phase === 'expired'}
	<div class="session-banner" role="status" data-testid="session-restore-banner">
		<span>Restore last session from {when}? ({sessionPlayed.doc?.count ?? 0} played)</span>
		<button type="button" disabled={busy} onclick={run(restoreLastSession)} data-testid="session-restore">Restore</button>
		<button type="button" disabled={busy} onclick={run(startNewSession)} data-testid="session-new">New session</button>
	</div>
{:else if sessionPlayed.phase === 'live' && sessionPlayed.visible.size > 0}
	<div class="session-live-anchor">
		<div class="session-line" data-testid="session-played-line">
			<span title="Tracks loaded this session keep a green title, across crashes and relaunches, until New session">{sessionPlayed.visible.size} played this session</span>
			<button type="button" class="link" disabled={busy} onclick={run(startNewSession)} data-testid="session-new">New session</button>
		</div>
	</div>
{/if}

<style>
	.session-banner {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 3px 8px;
		font-size: 11px;
		background: color-mix(in srgb, var(--rb-green) 16%, transparent);
		color: var(--rb-text);
	}
	.session-live-anchor {
		position: relative;
		z-index: 7;
		flex: 0 0 0;
		height: 0;
		overflow: visible;
	}
	.session-line {
		position: absolute;
		inset: 0 8px auto auto;
		display: flex;
		align-items: center;
		gap: 8px;
		box-sizing: border-box;
		padding: 2px 8px;
		border: 1px solid var(--rb-border);
		border-radius: 0 0 3px 3px;
		background: var(--rb-panel);
		color: var(--rb-text-dim);
		font-size: 11px;
		white-space: nowrap;
	}
	button {
		font: inherit;
		padding: 1px 8px;
		border-radius: 3px;
		border: 1px solid color-mix(in srgb, var(--rb-text-dim) 50%, transparent);
		background: transparent;
		color: inherit;
		cursor: pointer;
	}
	button.link {
		border: none;
		padding: 0;
		text-decoration: underline;
	}
</style>
