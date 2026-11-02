<script lang="ts">
	/**
	 * SETNOTE-6340/6340i/6340j/6340k: the floating Set notes pad. Mounted at the root
	 * (imported on first open, see SetNotesHost.svelte) so it floats over
	 * /performance too. The note itself is the live editor in NoteEditor.svelte;
	 * the bottom ribbon says whether it is saved and which shortcut applies to
	 * what is under the pointer, and the songs in the selection are compared
	 * off the pad's right-hand side so the note stays plain text.
	 */
	import { onMount } from 'svelte';
	import {
		ensureSetNoteLoaded,
		flushSetNote,
		listPlaylistScopes,
		loadSetNote,
		MIN_PAD_H,
		MIN_PAD_W,
		moveSetNotesPad,
		padUi,
		SCRATCH_SCOPE,
		setNote,
		setSetNotesOpen,
		toggleSetNotesDark,
		type PlaylistScope
	} from './store.svelte';
	import type { HoveredLink } from './editor';
	import { inGig } from './link-action';
	import NoteEditor from './NoteEditor.svelte';
	import TrackCompare from './TrackCompare.svelte';

	const HEADER_KEEP_VISIBLE_PX = 32;
	const COMPARE_W = 200;
	const COMPARE_GAP = 8;

	let playlists = $state<PlaylistScope[]>([]);
	let playlistsError = $state<string | null>(null);
	let linkError = $state<string | null>(null);
	let hovered = $state<HoveredLink | null>(null);
	let picked = $state<string[]>([]);

	const scopeOptions = $derived.by(() => {
		const opts = [{ value: SCRATCH_SCOPE, label: 'Scratch' }];
		for (const p of playlists) opts.push({ value: `playlist:${p.playlist_id}`, label: p.name });
		if (!opts.some((o) => o.value === padUi.scope)) {
			opts.push({ value: padUi.scope, label: padUi.scope });
		}
		return opts;
	});

	// The saved geometry may come from a wider window (or another machine), so
	// the pad is clamped where it is drawn, never by rewriting what was saved.
	let viewport = $state({ w: window.innerWidth, h: window.innerHeight });
	const _clamp = (v: number, max: number): number => Math.min(Math.max(0, v), Math.max(0, max));
	const box = $derived.by(() => {
		const w = Math.max(MIN_PAD_W, Math.min(padUi.w, viewport.w));
		const h = Math.max(MIN_PAD_H, Math.min(padUi.h, viewport.h));
		return { w, h, x: _clamp(padUi.x, viewport.w - w), y: _clamp(padUi.y, viewport.h - HEADER_KEEP_VISIBLE_PX) };
	});

	/** Right of the pad when it fits, else left of it. */
	const compareLeft = $derived(
		box.x + box.w + COMPARE_GAP + COMPARE_W <= viewport.w
			? box.x + box.w + COMPARE_GAP
			: Math.max(0, box.x - COMPARE_GAP - COMPARE_W)
	);

	const saveState = $derived(
		setNote.saving || setNote.dirty ? 'Saving\u2026' : setNote.rev === 0 ? 'New note' : 'Saved'
	);

	const tip = $derived.by(() => {
		if (hovered?.problem) return hovered.problem;
		const kind = hovered?.target.kind;
		if (kind === 'http') return 'Cmd/Ctrl-click opens the page in a new tab';
		if (kind) {
			const stems = kind === 'stem' ? ' with stems' : '';
			return inGig() ? `Press 1-4 to load onto that deck${stems} \u00b7 Cmd/Ctrl-click deck 1` : 'Cmd/Ctrl-click opens the track page';
		}
		if (picked.length === 1) return 'Shift-click past another song to compare';
		return '@ to mention a song \u00b7 right-click a word to link it \u00b7 right Cmd hides';
	});

	onMount(() => {
		ensureSetNoteLoaded();
		listPlaylistScopes()
			.then((rows) => (playlists = rows))
			.catch((error: unknown) => (playlistsError = error instanceof Error ? error.message : String(error)));
		const onResize = (): void => {
			viewport = { w: window.innerWidth, h: window.innerHeight };
		};
		window.addEventListener('resize', onResize);
		return () => {
			window.removeEventListener('resize', onResize);
			void flushSetNote();
		};
	});

	function _drag(e: PointerEvent, apply: (dx: number, dy: number) => void): void {
		if (e.button !== 0) return;
		e.preventDefault();
		const el = e.currentTarget as HTMLElement;
		el.setPointerCapture(e.pointerId);
		const sx = e.clientX;
		const sy = e.clientY;
		const move = (ev: PointerEvent): void => apply(ev.clientX - sx, ev.clientY - sy);
		const up = (): void => {
			el.removeEventListener('pointermove', move);
			el.removeEventListener('pointerup', up);
			el.removeEventListener('pointercancel', up);
			moveSetNotesPad({ x: padUi.x, y: padUi.y, w: padUi.w, h: padUi.h });
		};
		el.addEventListener('pointermove', move);
		el.addEventListener('pointerup', up);
		el.addEventListener('pointercancel', up);
	}

	function onHeaderPointerDown(e: PointerEvent): void {
		if ((e.target as HTMLElement).closest('button, select')) return;
		const { x: ox, y: oy, w, h } = box;
		_drag(e, (dx, dy) => {
			Object.assign(padUi, { w, h, x: _clamp(ox + dx, viewport.w - w), y: _clamp(oy + dy, viewport.h - HEADER_KEEP_VISIBLE_PX) });
		});
	}

	function onResizePointerDown(e: PointerEvent): void {
		const { x, y, w: ow, h: oh } = box;
		_drag(e, (dx, dy) => {
			Object.assign(padUi, {
				x,
				y,
				w: Math.max(MIN_PAD_W, Math.min(ow + dx, viewport.w - x)),
				h: Math.max(MIN_PAD_H, Math.min(oh + dy, viewport.h - y))
			});
		});
	}

	function onScopeChange(e: Event): void {
		void loadSetNote((e.currentTarget as HTMLSelectElement).value);
	}
</script>

<div class="sn-theme" class:dark={padUi.dark}>
<div
	class="sn-pad"
	data-testid="set-notes-pad"
	role="dialog"
	aria-label="Set notes"
	style:left="{box.x}px"
	style:top="{box.y}px"
	style:width="{box.w}px"
	style:height="{box.h}px"
>
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="sn-header" data-testid="set-notes-header" onpointerdown={onHeaderPointerDown}>
		<span class="sn-title">Set notes</span>
		<select
			class="sn-scope"
			data-testid="set-notes-scope"
			value={padUi.scope}
			onchange={onScopeChange}
			title={playlistsError ? `Playlists failed to load: ${playlistsError}` : 'Which set this note belongs to'}
		>
			{#each scopeOptions as opt (opt.value)}
				<option value={opt.value}>{opt.label}</option>
			{/each}
		</select>
		<button
			type="button"
			class="sn-icon"
			data-testid="set-notes-dark"
			aria-label="Dark paper"
			aria-pressed={padUi.dark}
			title={padUi.dark ? 'Back to yellow paper' : 'Dark paper, for a dark booth'}
			onclick={toggleSetNotesDark}
		>
			<svg viewBox="0 0 12 12" width="11" height="11" aria-hidden="true">
				<path d="M8.5 1.2A5 5 0 1 0 10.8 8 4 4 0 0 1 8.5 1.2Z" fill="currentColor" />
			</svg>
		</button>
		<button
			type="button"
			class="sn-icon"
			data-testid="set-notes-close"
			aria-label="Close set notes"
			title="Close (tap Right Cmd to reopen)"
			onclick={() => setSetNotesOpen(false)}
		>
			<svg viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">
				<path d="M1 1 9 9M9 1 1 9" stroke="currentColor" stroke-width="1.6" />
			</svg>
		</button>
	</div>

	<div class="sn-body">
		{#if setNote.loading}
			<p class="sn-status">Loading&hellip;</p>
		{:else}
			<NoteEditor onError={(message) => (linkError = message)} bind:hovered bind:picked />
		{/if}
	</div>

	<div class="sn-footer">
		{#if setNote.error || linkError}
			<span class="sn-grow sn-error" data-testid="set-notes-error" title={setNote.error ?? linkError ?? ''}
				>{setNote.error ?? linkError}</span
			>
		{:else}
			<span
				data-testid="set-notes-status"
				data-rev={setNote.rev}
				title="Saved revision {setNote.rev} ({setNote.rev === 0
					? 'not saved yet, the first edit creates it'
					: 'saved to this library'}), {setNote.body.length} characters of markdown"
				>{saveState}</span
			>
			<span class="sn-grow sn-tip" data-testid="set-notes-tip">{tip}</span>
		{/if}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<span
			class="sn-resize"
			data-testid="set-notes-resize"
			title="Drag to resize"
			onpointerdown={onResizePointerDown}
		></span>
	</div>
</div>
{#if picked.length > 0}
	<TrackCompare ids={picked} left={compareLeft} top={box.y} />
{/if}
</div>

<style>
	.sn-theme {
		display: contents;
		--sn-paper: #fff59d;
		--sn-head: #fbe56b;
		--sn-soft: #fffde7;
		--sn-hl: #fbe56b;
		--sn-line: #d4c45a;
		--sn-ink: #2b2610;
		--sn-muted: #6b5f1a;
		--sn-link: #1d3f9a;
		--sn-err: #a01818;
	}
	.sn-theme.dark {
		--sn-paper: #1f1d14;
		--sn-head: #2a2716;
		--sn-soft: #26231a;
		--sn-hl: #3d3818;
		--sn-line: #4a4422;
		--sn-ink: #e9dc9a;
		--sn-muted: #9a8f5c;
		--sn-link: #9ec1ff;
		--sn-err: #ff8a80;
	}
	.sn-pad {
		position: fixed;
		z-index: 350;
		display: flex;
		flex-direction: column;
		background: var(--sn-paper);
		color: var(--sn-ink);
		border: 1px solid var(--sn-line);
		border-radius: 4px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
		font-size: 13px;
		overflow: hidden;
	}
	.sn-header {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 4px 6px;
		background: var(--sn-head);
		border-bottom: 1px solid var(--sn-line);
		cursor: move;
		user-select: none;
		touch-action: none;
	}
	.sn-title {
		font-weight: 700;
		white-space: nowrap;
	}
	.sn-scope {
		flex: 1 1 auto;
		min-width: 0;
		background: var(--sn-soft);
		color: inherit;
		border: 1px solid var(--sn-line);
		font-size: 12px;
	}
	.sn-icon {
		display: grid;
		place-items: center;
		height: 20px;
		padding: 0 6px;
		background: transparent;
		color: inherit;
		border: 1px solid var(--sn-line);
		border-radius: 3px;
		cursor: pointer;
	}
	.sn-body {
		position: relative;
		flex: 1 1 auto;
		min-height: 0;
		display: flex;
	}
	.sn-status {
		padding: 6px 10px;
		margin: 0;
	}
	.sn-footer {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 2px 6px;
		font-size: 11px;
		border-top: 1px solid var(--sn-line);
		min-height: 18px;
	}
	.sn-grow {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.sn-tip {
		color: var(--sn-muted);
	}
	.sn-error {
		color: var(--sn-err);
	}
	.sn-resize {
		flex: 0 0 14px;
		height: 14px;
		cursor: nwse-resize;
		touch-action: none;
		background: linear-gradient(135deg, transparent 50%, var(--sn-line) 50%, var(--sn-line) 60%, transparent 60%, transparent 70%, var(--sn-line) 70%, var(--sn-line) 80%, transparent 80%);
	}
</style>
