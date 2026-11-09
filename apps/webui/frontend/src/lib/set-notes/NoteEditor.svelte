<script lang="ts">
	/**
	 * SETNOTE-6340/6340d/6340e/6340f/6340i: the live-rendered note. The ProseMirror document is
	 * the editing model; markdown (./editor/markdown.ts) is what is saved, so
	 * every doc change is serialized into the store's debounced save. A body
	 * that changes underneath (scope switch, 409 reload) replaces the doc; the
	 * last serialized string guards typing from resetting itself.
	 *
	 * It reports the link under the pointer and the songs in the selection
	 * (`hovered`, `picked`) for the pad's ribbon and compare panel. In Gig, a
	 * digit 1-4 loads the hovered song onto that deck, but only when the
	 * pointer moved onto it since the last key, so typing a digit with the
	 * mouse resting on a link still types it.
	 */
	import { onMount, untrack } from 'svelte';
	import { toggleMark } from 'prosemirror-commands';
	import { EditorState } from 'prosemirror-state';
	import { EditorView } from 'prosemirror-view';
	import type { DeckId } from '$lib/rb/deck-id';
	import { editSetNote, setNote, trackExists, type TrackHit } from './store.svelte';
	import {
		createTrackState,
		editorPlugins,
		insertMentionLink,
		linkAt,
		linkPlugin,
		linkRange,
		mentionPlugin,
		noteSchema,
		menuTargetAt,
		parseNoteMarkdown,
		selectWordAt,
		serializeNoteMarkdown,
		trackIdsInSelection,
		unlinkRange,
		type HoveredLink,
		type Mention,
		type NoteTarget
	} from './editor';
	import { inGig, openNoteTarget } from './link-action';
	import { isPlainDeckKey } from './hotkey/deck-key';
	import { createTrackSearch, hitLabel } from './track-search.svelte';
	import LinkMenu from './LinkMenu.svelte';

	let {
		onError,
		hovered = $bindable(null),
		picked = $bindable([])
	}: { onError: (message: string | null) => void; hovered?: HoveredLink | null; picked?: string[] } = $props();

	type Menu = {
		x: number;
		y: number;
		from: number;
		to: number;
		text: string;
		link: ReturnType<typeof linkAt>;
		/** SETNOTE-10457: a text link's or a chip's target, null on plain text. */
		target: NoteTarget | null;
	};

	const MENTION_HITS = 8;
	const DECK_KEYS: Record<string, DeckId> = { '1': 1, '2': 2, '3': 3, '4': 4 };
	const tracks = createTrackState(trackExists);
	const search = createTrackSearch(MENTION_HITS);
	const plugins = editorPlugins([
		mentionPlugin({ change: onMention, key: onMentionKey }),
		linkPlugin({ follow: (target) => void act(target), contextMenu: openMenu, hover: onHover }),
		tracks.plugin
	]);
	let host = $state<HTMLDivElement | null>(null);
	let view: EditorView | null = null;
	let lastMd = '';
	let mention = $state<Mention | null>(null);
	let hitIndex = $state(0);
	let menu = $state<Menu | null>(null);
	let armed = false;

	const stateFor = (md: string): EditorState => EditorState.create({ doc: parseNoteMarkdown(md), plugins });

	onMount(() => {
		lastMd = setNote.body;
		const v: EditorView = new EditorView(host!, {
			state: stateFor(lastMd),
			dispatchTransaction(tr) {
				v.updateState(v.state.apply(tr));
				if (tr.selectionSet || tr.docChanged) setPicked(trackIdsInSelection(v.state));
				if (!tr.docChanged) return;
				const md = serializeNoteMarkdown(v.state.doc);
				if (md === lastMd) return;
				lastMd = md;
				editSetNote(md);
			}
		});
		view = v;
		window.addEventListener('keydown', onDeckKey, true);
		return () => {
			window.removeEventListener('keydown', onDeckKey, true);
			search.cancel();
			view = null;
			v.destroy();
		};
	});

	$effect(() => {
		const body = setNote.body;
		untrack(() => {
			if (view === null || body === lastMd) return;
			lastMd = body;
			view.updateState(stateFor(body));
		});
	});

	async function act(target: NoteTarget, deck?: DeckId): Promise<void> {
		if (!tracks.usable(target)) return;
		onError(null);
		try {
			await openNoteTarget(target, deck);
		} catch (error) {
			onError(`Could not open ${'url' in target ? target.url : target.stableId}: ${error instanceof Error ? error.message : String(error)}`);
		}
	}

	const _key = (t: NoteTarget | null): string => (t === null ? '' : 'url' in t ? t.url : `${t.kind}:${t.stableId}`);

	function onHover(target: NoteTarget | null): void {
		armed = target !== null;
		if (_key(target) !== _key(hovered?.target ?? null)) {
			hovered = target && { target, problem: tracks.problem(target) };
		}
	}

	function setPicked(ids: string[]): void {
		if (ids.join() !== picked.join()) picked = ids;
	}

	function onDeckKey(event: KeyboardEvent): void {
		const deck = DECK_KEYS[event.key];
		const plain = isPlainDeckKey(event);
		const target = armed && plain && inGig() ? (hovered?.target ?? null) : null;
		armed = false;
		if (deck === undefined || target === null || target.kind === 'http') return;
		event.preventDefault();
		event.stopPropagation();
		void act(target, deck);
	}

	function onMention(next: Mention | null): void {
		mention = next;
		hitIndex = 0;
		search.query(next?.query ?? '');
	}

	function onMentionKey(event: KeyboardEvent): boolean {
		const hits = search.state.hits;
		if (hits.length === 0) return false;
		if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
			hitIndex = (hitIndex + (event.key === 'ArrowDown' ? 1 : hits.length - 1)) % hits.length;
		} else {
			pickHit(hits[hitIndex]);
		}
		return true;
	}

	function pickHit(hit: TrackHit): void {
		if (!view || !mention) return;
		view.dispatch(insertMentionLink(view.state, mention, `track:${hit.stable_id}`, hit.title || hitLabel(hit)));
		view.focus();
	}

	function openMenu(event: MouseEvent): void {
		if (!view) return;
		event.preventDefault();
		const clickPos = view.posAtCoords({ left: event.clientX, top: event.clientY })?.pos ?? null;
		// SETNOTE-10457: read a chip under the click before the word select moves the selection.
		const chipHit = clickPos === null ? null : menuTargetAt(view.state, clickPos);
		const select = selectWordAt(view.state, clickPos);
		if (select) view.dispatch(select);
		const { from, to } = view.state.selection;
		const text = view.state.doc.textBetween(from, to, ' ', '').trim();
		const hit = menuTargetAt(view.state, from) ?? chipHit;
		menu = { x: event.clientX, y: event.clientY, from, to, text, link: hit?.link ?? null, target: hit?.target ?? null };
	}

	function closeMenu(then?: (v: EditorView, m: Menu) => void): void {
		const m = menu;
		menu = null;
		if (!view || !m) return;
		then?.(view, m);
		view.focus();
	}

	const linkTo = (hit: TrackHit): void =>
		closeMenu((v, m) => v.dispatch(linkRange(v.state, m, `track:${hit.stable_id}`)));
	const bold = (): void => closeMenu((v) => toggleMark(noteSchema.marks.strong)(v.state, v.dispatch));
	const openLink = (): void =>
		closeMenu((_v, m) => {
			if (m.target) void act(m.target);
		});
	const removeLink = (): void =>
		closeMenu((v, m) => m.link && v.dispatch(unlinkRange(v.state, m.link)));
</script>

<div class="sn-editor" data-testid="set-notes-editor" bind:this={host}></div>

{#if mention && search.state.hits.length > 0}
	<ul
		class="sn-search"
		data-testid="set-notes-search"
		role="listbox"
		style:left="{Math.min(mention.left, window.innerWidth - 280)}px"
		style:top="{mention.bottom + 2}px"
	>
		{#each search.state.hits as hit, i (hit.stable_id)}
			<li role="option" aria-selected={i === hitIndex}>
				<button
					type="button"
					class:active={i === hitIndex}
					data-testid="set-notes-search-hit"
					onpointerdown={(e) => e.preventDefault()}
					onclick={() => pickHit(hit)}>{hitLabel(hit)}</button
				>
			</li>
		{/each}
	</ul>
{:else if mention && search.state.error}
	<p class="sn-search sn-search-error" style:left="{mention.left}px" style:top="{mention.bottom + 2}px">
		Search failed: {search.state.error}
	</p>
{/if}

{#if menu}
	<LinkMenu
		x={menu.x}
		y={menu.y}
		text={menu.text}
		canLink={menu.from < menu.to}
		onLink={linkTo}
		onBold={bold}
		linkActions={menu.target
			? { open: openLink, remove: menu.link ? removeLink : null, problem: tracks.problem(menu.target) }
			: null}
		onClose={() => closeMenu()}
	/>
{/if}

<style>
	.sn-editor {
		flex: 1 1 auto;
		min-width: 0;
		overflow: auto;
	}
	.sn-editor :global(.ProseMirror) {
		min-height: 100%;
		padding: 6px 10px;
		line-height: 22px;
		outline: none;
		white-space: pre-wrap;
		word-wrap: break-word;
		font-variant-ligatures: none;
	}
	.sn-editor :global(:is(h1, h2, h3, p, ul)) {
		margin: 0;
		line-height: 22px;
		font-size: 13px;
	}
	.sn-editor :global(h1) {
		font-size: 16px;
	}
	.sn-editor :global(h2) {
		font-size: 14px;
	}
	.sn-editor :global(ul) {
		padding-left: 18px;
	}
	.sn-editor :global(:is(.sn-link, .sn-chip)) {
		color: var(--sn-link);
		text-decoration: underline;
		text-underline-offset: 2px;
		cursor: text;
	}
	.sn-editor :global(:is(.sn-link, .sn-chip):hover) {
		text-decoration-thickness: 2px;
	}
	.sn-editor :global(.sn-chip-stem) {
		text-decoration-style: dashed;
	}
	.sn-editor :global(.sn-missing) {
		opacity: 0.55;
		text-decoration: line-through;
		cursor: help;
	}
	.sn-editor :global(.ProseMirror-selectednode) {
		outline: 1px solid var(--sn-line);
	}
	.sn-search {
		position: fixed;
		z-index: 360;
		width: 270px;
		margin: 0;
		padding: 2px;
		list-style: none;
		max-height: 220px;
		overflow: auto;
		background: var(--sn-soft);
		color: var(--sn-ink);
		border: 1px solid var(--sn-line);
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
		font-size: 12px;
	}
	.sn-search button {
		display: block;
		width: 100%;
		text-align: left;
		background: transparent;
		color: inherit;
		border: 0;
		padding: 2px 6px;
		cursor: pointer;
		font: inherit;
	}
	.sn-search button.active {
		background: var(--sn-hl);
	}
	.sn-search-error {
		color: var(--sn-err);
	}
</style>
