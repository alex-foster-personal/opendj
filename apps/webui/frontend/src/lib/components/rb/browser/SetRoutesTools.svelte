<script lang="ts">
	/**
	 * SET-6586 set-tab tools: the Routes toggle and Make component, plus the
	 * component load the tab glyphs read. Loaded lazily by PlaylistSetTabs and
	 * only when flag `sets.routes_view` is on, so it stays out of the
	 * /performance eager bundle.
	 */
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import {
		createPlaylistComponent,
		listPlaylistComponents,
		type PlaylistSet
	} from '$lib/rb/api-playlist-sets';
	import type { BrowserRow } from './pane-contract.svelte';
	import {
		componentGlyphs,
		DEFAULT_SLOTS,
		matchSet,
		openingPick,
		parseTags,
		partsOf,
		pickFromParts,
		slotGroups
	} from './set-routes';
	import { setRoutesView } from './set-routes-view.svelte';

	let {
		playlistId,
		rows,
		selectedOrders,
		sets
	}: {
		playlistId: string;
		rows: readonly BrowserRow[];
		selectedOrders: readonly number[];
		sets: readonly PlaylistSet[];
	} = $props();

	const components = $derived(setRoutesView.components);
	const slotChoices = $derived([
		...new Set([...DEFAULT_SLOTS, ...components.filter((c) => !c.borrowed).map((c) => c.slot)])
	]);
	/** The selected rows by membership position, so a repeated track keeps only the copies picked. */
	const selectedRows = $derived.by(() => {
		if (selectedOrders.length === 0) return [];
		const picked = new Set(selectedOrders);
		return rows.filter((r) => picked.has(r.order)).sort((a, b) => a.order - b.order);
	});
	/** The selection in playlist order, so a component keeps the playlist's order. */
	const selectionInOrder = $derived(selectedRows.map((r) => r.stable_id));
	/** A track that repeats outside the selection cannot be saved by id alone, so the component is refused. */
	const splitTrack = $derived.by(() => {
		const picked = new Set(selectedOrders);
		const inSelection = new Set(selectionInOrder);
		return rows.some((r) => !picked.has(r.order) && inSelection.has(r.stable_id));
	});
	let making = $state(false);
	let compName = $state('');
	let compSlot = $state<string>('opener');
	let compTags = $state('');
	let compError = $state<string | null>(null);

	function errorText(exc: unknown): string {
		return exc instanceof Error ? exc.message : String(exc);
	}

	async function refreshComponents(): Promise<void> {
		const id = playlistId;
		try {
			const comps = await listPlaylistComponents(id);
			if (setRoutesView.playlistId !== id) return;
			setRoutesView.components = comps;
			setRoutesView.glyphs = componentGlyphs(comps);
			if (setRoutesView.pickPending) {
				// The set clicked before these arrived: light its route now, not the opening one.
				const clicked = setRoutesView.sets.find((s) => s.id === setRoutesView.selectedSetId);
				const next = clicked ? pickFromParts(clicked.parts, comps) : {};
				setRoutesView.pick = Object.keys(next).length > 0 ? next : null;
				setRoutesView.pickPending = false;
			}
		} catch (exc) {
			if (id === playlistId) compError = errorText(exc);
		}
	}

	async function handleMakeComponent(): Promise<void> {
		const name = compName.trim();
		if (!name || selectionInOrder.length === 0 || splitTrack) return;
		try {
			await createPlaylistComponent(playlistId, {
				name,
				slot: compSlot,
				stable_ids: selectionInOrder,
				tags: parseTags(compTags)
			});
			compName = '';
			compTags = '';
			compError = null;
			making = false;
			await refreshComponents();
		} catch (exc) {
			compError = errorText(exc);
		}
	}

	/** The set whose route is lit in Routes mode, highlighted like the design. */
	$effect(() => {
		if (!setRoutesView.active) {
			setRoutesView.routeSetId = null;
			return;
		}
		const groups = slotGroups(components);
		const pick = setRoutesView.pick ?? openingPick(groups, components, sets);
		setRoutesView.routeSetId = matchSet(partsOf(pick, groups), sets)?.id ?? null;
	});

	$effect(() => {
		const id = playlistId;
		const uncomps = subscribeKind('playlist_components', (ids) => {
			if (ids.includes(id)) void refreshComponents();
		});
		const unresync = subscribeResync(() => {
			void refreshComponents();
		});
		void refreshComponents();
		return () => {
			uncomps();
			unresync();
		};
	});

	// Flag turned off or tabs gone: leave Routes mode so the track table returns.
	$effect(() => () => {
		setRoutesView.active = false;
	});
</script>

<button
	type="button"
	class="routes-toggle"
	data-testid="set-routes-toggle"
	aria-pressed={setRoutesView.active}
	class:pressed={setRoutesView.active}
	title="Show this playlist as components and the routes through them"
	onclick={() => {
		setRoutesView.active = !setRoutesView.active;
	}}
>
	Routes
</button>
<button
	type="button"
	data-testid="make-component"
	disabled={selectionInOrder.length === 0 || splitTrack}
	title={selectionInOrder.length === 0
		? 'Select a run of tracks in the playlist first'
		: splitTrack
			? 'A selected track also appears outside the selection; select every copy of it, or none'
			: `Make a component from the ${selectionInOrder.length} selected track(s)`}
	onclick={() => {
		making = !making;
	}}
>
	Make component
</button>
{#if making}
	<form
		class="make-row"
		data-testid="make-component-form"
		onsubmit={(e) => {
			e.preventDefault();
			void handleMakeComponent();
		}}
	>
		<span title="Selected tracks that will form the component">{selectionInOrder.length}</span> tracks as
		<input type="text" placeholder="Component name" bind:value={compName} data-testid="make-component-name" />
		<label
			>slot
			<select bind:value={compSlot} data-testid="make-component-slot">
				{#each slotChoices as slot (slot)}
					<option value={slot}>{slot}</option>
				{/each}
			</select></label
		>
		<input type="text" placeholder="#tags" bind:value={compTags} data-testid="make-component-tags" />
		<button type="submit" data-testid="make-component-save" disabled={!compName.trim() || selectionInOrder.length === 0}
			>Save component</button
		>
		<button
			type="button"
			onclick={() => {
				making = false;
			}}>Cancel</button
		>
	</form>
{/if}
{#if compError}<span class="make-error" role="alert">{compError}</span>{/if}

<style>
	.routes-toggle.pressed {
		border-color: var(--rb-accent);
		background: color-mix(in srgb, var(--rb-panel) 70%, var(--rb-accent) 30%);
	}
	.make-row {
		order: 10;
		flex-basis: 100%;
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		align-items: center;
		padding-top: 4px;
		font-size: var(--rb-fs-label);
		border-top: 1px solid color-mix(in srgb, var(--rb-border) 70%, transparent);
	}
	.make-row input {
		width: 9rem;
		font-size: var(--rb-fs-label);
	}
	.make-error {
		color: var(--rb-red);
		font-size: var(--rb-fs-label);
	}
</style>
