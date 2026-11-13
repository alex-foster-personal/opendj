<script lang="ts">
	/**
	 * Bottom-left connectivity strip: soft Spotify/Rekordbox/djay above bright
	 * LIB/BE/FE, with a pinable keyboard-reachable hover panel (issue #11172).
	 */
	import {
		CONN_DOT_POLICY,
		connTipFromHealthDot,
		formatCopyAllTips,
		shouldShowAskAgent,
		softDotTip,
		type ConnDotTip,
		type SoftDotState
	} from '$lib/rb/conn-dot-tips';
	import type { LibraryHealthDot } from '$lib/rb/library-health-dots';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { pushToast } from '$lib/stores.svelte';

	type Props = {
		libDot: LibraryHealthDot;
		beDot: LibraryHealthDot;
		feDot: LibraryHealthDot;
		softDots: SoftDotState[];
	};

	let { libDot, beDot, feDot, softDots }: Props = $props();

	let wrapEl = $state<HTMLDivElement | undefined>();
	let hoverId = $state<string | null>(null);
	let open = $state(false);
	let pinned = $state(false);
	let hoverTimer: ReturnType<typeof setTimeout> | null = null;
	const HOVER_DELAY_MS = 120;

	const allTips = $derived.by(() => {
		const out: ConnDotTip[] = [];
		for (const dot of softDots) out.push(softDotTip(dot));
		for (const dot of [libDot, beDot, feDot]) {
			const tip = connTipFromHealthDot(dot);
			if (tip !== null) out.push(tip);
		}
		return out;
	});

	const tipsById = $derived.by(() => {
		const m = new Map<string, ConnDotTip>();
		for (const tip of allTips) m.set(tip.id, tip);
		return m;
	});

	const activeTip = $derived(hoverId !== null ? (tipsById.get(hoverId) ?? null) : null);

	function _openNow(id: string): void {
		hoverId = id;
		open = true;
	}

	function onEnter(id: string): void {
		if (hoverTimer !== null) clearTimeout(hoverTimer);
		hoverTimer = setTimeout(() => _openNow(id), HOVER_DELAY_MS);
	}

	function onLeaveStrip(): void {
		if (pinned) return;
		if (hoverTimer !== null) {
			clearTimeout(hoverTimer);
			hoverTimer = null;
		}
		open = false;
		hoverId = null;
	}

	function onFocusDot(id: string): void {
		if (hoverTimer !== null) {
			clearTimeout(hoverTimer);
			hoverTimer = null;
		}
		_openNow(id);
	}

	function onFocusOut(e: FocusEvent): void {
		if (pinned) return;
		const next = e.relatedTarget as Node | null;
		if (next !== null && wrapEl?.contains(next)) return;
		open = false;
		hoverId = null;
	}

	function onKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape') {
			pinned = false;
			open = false;
			hoverId = null;
			wrapEl?.focus();
		}
	}

	function togglePin(): void {
		pinned = !pinned;
		if (!pinned && hoverId === null) open = false;
	}

	function closePanel(): void {
		pinned = false;
		open = false;
		hoverId = null;
	}

	function onOutsidePointerDown(event: PointerEvent): void {
		if (!open || pinned || event.button !== 0) return;
		const target = event.target;
		if (!(target instanceof Node) || wrapEl?.contains(target)) return;
		open = false;
		hoverId = null;
	}

	async function copyAllTips(): Promise<void> {
		const text = formatCopyAllTips(allTips);
		try {
			await navigator.clipboard.writeText(text);
			pushToast('Copied all conn-dot tips', 'info');
		} catch {
			pushToast('Clipboard blocked; select text from the panel', 'error');
		}
	}
</script>

<div
	class="conn-dots"
	role="group"
	aria-label="server connectivity"
	data-testid="conn-dots-strip"
	data-custom-tip=""
	bind:this={wrapEl}
	tabindex="-1"
	onmouseleave={onLeaveStrip}
	onfocusout={onFocusOut}
	onkeydown={onKeydown}
>
	{#each softDots as dot (dot.id)}
		{@const tip = softDotTip(dot)}
		<button
			type="button"
			class="conn-dot soft"
			class:up={tip.status === 'up'}
			class:down={tip.status === 'down'}
			class:gray={tip.status === 'unknown'}
			class:off={tip.status === 'off'}
			data-server={dot.id}
			data-testid={`conn-dot-${dot.id}`}
			data-custom-tip=""
			aria-label={`${tip.name} ${tip.status}`}
			onmouseenter={() => onEnter(dot.id)}
			onfocus={() => onFocusDot(dot.id)}
		></button>
	{/each}
	{#each [{ id: 'lib', dot: libDot }, { id: 'be', dot: beDot }, { id: 'fe', dot: feDot }] as row (row.id)}
		{@const tip = connTipFromHealthDot(row.dot)}
		{#if tip !== null}
			<button
				type="button"
				class="conn-dot bright"
				class:up={tip.status === 'up'}
				class:down={tip.status === 'down'}
				class:gray={tip.status === 'unknown'}
				class:off={tip.status === 'off'}
				data-server={row.id}
				data-testid={`conn-dot-${row.id}`}
				data-custom-tip=""
				aria-label={`${tip.name} ${tip.status}`}
				onmouseenter={() => onEnter(row.id)}
				onfocus={() => onFocusDot(row.id)}
			></button>
		{/if}
	{/each}

	{#if open && activeTip}
		<div
			class="conn-panel"
			data-testid="conn-dot-popover"
			data-hover-card=""
			use:triggerFloatingAction={{ getTrigger: () => wrapEl ?? null, preferred: 'above', gap: 6 }}
		>
			<div class="conn-panel-head">
				<span class="conn-panel-name">{activeTip.name}</span>
				<span class="conn-panel-st" class:bad={!activeTip.up}>{activeTip.status}</span>
				<button
					type="button"
					class="conn-panel-pin"
					class:active={pinned}
					data-testid="conn-dot-pin"
					aria-pressed={pinned}
					aria-label={pinned ? 'Unpin conn-dot panel' : 'Pin conn-dot panel open'}
					onclick={togglePin}
				>
					{pinned ? 'Pinned' : 'Pin'}
				</button>
				<button
					type="button"
					class="conn-panel-close"
					data-testid="conn-dot-close"
					aria-label="Close conn-dot panel"
					onclick={closePanel}
				>
					Close
				</button>
			</div>
			<p class="conn-panel-m">{activeTip.meaning}</p>
			{#if shouldShowAskAgent(activeTip) && activeTip.askAgent && activeTip.askAgent.length > 0}
				<ul class="conn-panel-ask" data-testid="conn-dot-ask">
					{#each activeTip.askAgent as line}
						<li>{line}</li>
					{/each}
				</ul>
			{/if}
			<p class="conn-panel-policy" data-testid="conn-dot-policy">{CONN_DOT_POLICY}</p>
			<button
				type="button"
				class="conn-panel-copy"
				data-testid="conn-dot-copy-all"
				onclick={() => void copyAllTips()}
			>
				Copy all tips
			</button>
			<p class="conn-panel-hint">
				Soft = light (non-blocking). Bright = LIB/BE/FE from library health. Coverage dots stay in
				the bottom tray.
			</p>
		</div>
	{/if}
</div>

<svelte:window onpointerdowncapture={onOutsidePointerDown} />

<style>
	.conn-dots {
		position: absolute;
		/* Past the 30px icon rail (UX-RAIL-01); left: 10px overlapped
		 * bottom-corner rail buttons on short /performance browser rows. */
		left: 38px;
		bottom: 34px;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 3px;
		z-index: 60;
		pointer-events: auto;
		outline: none;
	}
	.conn-dot {
		width: 12px;
		height: 12px;
		padding: 0;
		border: none;
		border-radius: 50%;
		background: #3a4048;
		box-shadow: inset 0 0 0 1px #23282f;
		cursor: help;
	}
	.conn-dots:focus-visible {
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}
	.conn-dot:focus-visible {
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}
	.conn-panel-pin:focus-visible,
	.conn-panel-close:focus-visible,
	.conn-panel-copy:focus-visible {
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}
	.conn-dot.up {
		background: var(--rb-green, #35c04f);
		box-shadow: 0 0 4px color-mix(in srgb, var(--rb-green, #35c04f) 70%, transparent);
	}
	.conn-dot.soft.up {
		background: color-mix(in srgb, var(--rb-green, #35c04f) 55%, #c8f5d0);
		box-shadow: 0 0 3px color-mix(in srgb, var(--rb-green, #35c04f) 40%, transparent);
	}
	.conn-dot.down {
		background: var(--rb-red, #d9534f);
	}
	.conn-dot.gray {
		background: var(--rb-text-dim, #9aa3ad);
	}
	.conn-dot.off {
		background: color-mix(in srgb, var(--rb-orange) 70%, #3a4048);
		opacity: 0.85;
	}
	.conn-panel {
		position: fixed;
		width: min(340px, 70vw);
		padding: 8px 10px;
		border: 1px solid var(--rb-border, #2e333a);
		border-radius: 4px;
		background: color-mix(in srgb, var(--rb-panel, #1a1e24) 94%, #000);
		color: var(--rb-text, #d7dde5);
		font-size: 11px;
		line-height: 1.35;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
		/* Unpinned hover tips must not eat library / deck clicks underneath (#13523). */
		pointer-events: none;
		z-index: 9600;
	}
	.conn-panel:has(.conn-panel-pin.active) {
		pointer-events: auto;
	}
	.conn-panel-head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		margin-bottom: 4px;
		font-weight: 700;
	}
	.conn-panel-name {
		flex: 1 1 auto;
		min-width: 8rem;
	}
	.conn-panel-st {
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--rb-green, #35c04f);
		font-size: 10px;
	}
	.conn-panel-st.bad {
		color: #e07070;
	}
	.conn-panel :is(.conn-panel-pin, .conn-panel-close, .conn-panel-copy) {
		pointer-events: auto;
	}
	.conn-panel-pin,
	.conn-panel-close,
	.conn-panel-copy {
		border: 1px solid #3a4048;
		border-radius: 3px;
		background: #12161c;
		color: var(--rb-text, #d7dde5);
		font: inherit;
		font-size: 10px;
		padding: 3px 6px;
		cursor: pointer;
	}
	.conn-panel-pin.active {
		border-color: var(--rb-accent, #4fa3ff);
	}
	.conn-panel-m {
		margin: 0 0 6px;
		color: var(--rb-text-dim, #9aa3ad);
	}
	.conn-panel-ask {
		margin: 0 0 6px;
		padding-left: 1.1em;
		color: var(--rb-text, #d7dde5);
	}
	.conn-panel-ask li {
		margin-bottom: 3px;
	}
	.conn-panel-policy {
		margin: 0 0 6px;
		padding: 4px 6px;
		border-left: 2px solid var(--rb-accent, #4fa3ff);
		color: var(--rb-text, #d7dde5);
		font-size: 10px;
	}
	.conn-panel-copy {
		margin-bottom: 6px;
	}
	.conn-panel-hint {
		margin: 0;
		color: var(--rb-text-dim, #9aa3ad);
		font-size: 10px;
	}
</style>
