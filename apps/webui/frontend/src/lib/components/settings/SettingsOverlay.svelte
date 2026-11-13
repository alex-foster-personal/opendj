<script lang="ts">
	/**
	 * Warp-style searchable settings overlay.
	 * Opens like a command palette, expands for results; RHS rows ARE the settings.
	 */
	import { tick, untrack } from 'svelte';
	import { focusTrap } from '$lib/keyboard/focus-trap';
	import SettingsAiAskView from './SettingsAiAskView.svelte';
	import SettingsHeaderView from './SettingsHeaderView.svelte';
	import SettingPreview from './SettingPreview.svelte';
	import GroupPad from '$lib/components/rb/mixer/GroupPad.svelte';
	import { DRAG_DEFAULTS, dragSensitivity } from '$lib/rb/drag-sensitivity.svelte';
	import { tierOnNowLabel } from '$lib/rb/perf-tier';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import {
		RUN_SETUP_LABEL,
		RUN_SETUP_TITLE,
		runSetup,
		runSetupBlocked
	} from '$lib/setup/run-setup';
	import {
		SETTING_GROUPS,
		SETTINGS_CATALOG,
		type SettingDef,
		type SettingGroupId
	} from '$lib/settings/catalog';
	import { applySettingChange, readSettingValue, settingUnavailable, type AllowedSettingKey } from '$lib/settings/apply';
	import { engineRestartNeeded, restartToLoadEngine } from '$lib/audio-engine/rust-mode.svelte';
	import {
		applyBooleanAction,
		booleanKeyAction,
		clampIndex,
		moveSelection
	} from '$lib/settings/keyboard';
	import {
		canHistoryBack,
		canHistoryForward,
		closeSettings,
		commitSettingsSearch,
		historyBack,
		historyForward,
		setSettingsGroup,
		setSettingsQuery,
		setSettingsSelectedIndex,
		settingsOverlay
	} from '$lib/settings/overlay.svelte';
	import {
		effectiveHideTodo,
		filterSettings,
		settingRowVisible,
		visibleGroups
	} from '$lib/settings/search';
	import { startsPanelDrag, type DragNode } from '$lib/rb/feedback';
	import { attachSettingsPanel, clampPanelOffset, type PanelOffset } from '$lib/settings/panel-offset';
	import { subscribeWheelSensitivity } from '$lib/rb/wheel-adjust';
	import {
		setAutoSyncDestination,
		setDragSensitivity,
		uiPrefs,
		type AutoSyncDestination
	} from '$lib/rb/prefs.svelte';

	const INERT = 'not implemented - see PARITY-TODO';

	let searchEl = $state<HTMLInputElement | null>(null);
	let aiIds = $state<string[]>([]);
	let aiPending = $state(false);
	let aiError = $state<string | null>(null);
	let aiSeq = 0;
	let applyBusy = $state(false);
	let applyMsg = $state<string | null>(null);
	let applyOk = $state<boolean | null>(null);
	let pendingProposal = $state<{
		key: string;
		value: boolean | string;
		rationale: string;
	} | null>(null);

	/** Local mirror of the number-kind rows (today: the two wheel-sensitivity
	 * factors). Their store lives in `lib/rb/wheel-adjust.ts`, which is a plain
	 * .ts file and therefore carries no runes - Svelte cannot track a plain
	 * module variable. Seeded on open, and refreshed by the subscription below
	 * on EVERY change, including writes this component did not make. */
	let numberDraft = $state<Record<string, number>>({});

	// Todo rows and developer-only rows are hidden unless "Show developer
	// pages" (show_dev_ui) is on - V1 hide-unbuilt-UI, JIK Thu 1 Oct 2026.
	const showDev = $derived(uiPrefs.show_dev_ui);
	const hideTodo = $derived(effectiveHideTodo(uiPrefs));
	const filterOpts = $derived({
		hideTodo,
		showDev,
		group: settingsOverlay.group as SettingGroupId | null,
		aiIds
	});
	const filtered = $derived(filterSettings(settingsOverlay.query, filterOpts));
	const groupsShown = $derived(visibleGroups(settingsOverlay.query, { hideTodo, showDev, aiIds }));
	const selected = $derived(
		filtered.all[clampIndex(settingsOverlay.selectedIndex, filtered.all.length)] ?? null
	);
	const aiIdSet = $derived(new Set(aiIds));
	const expanded = $derived(settingsOverlay.query.trim().length > 0 || filtered.all.length > 0);
	/** Saved engine differs from the one this page already started. */
	const showEngineRestart = $derived(engineRestartNeeded());

	/** Offset from the centered slot. Plain numbers, written onto the panel as
	 * CSS variables, so a drag does not re-render the settings list. */
	let panelEl = $state<HTMLDivElement | null>(null);
	let headDragging = $state(false);
	let panelDx = 0;
	let panelDy = 0;
	let panelDrag: { x: number; y: number; dx: number; dy: number } | null = null;

	function onHeadPointerDown(e: PointerEvent): void {
		if (e.button !== 0) return;
		// Back, forward, search, and close keep their own clicks. The grip and
		// the rest of the header start a move (pin 0970ee393b96).
		if (!startsPanelDrag(e.target as DragNode | null, e.currentTarget as DragNode | null)) return;
		e.preventDefault();
		panelDrag = { x: e.clientX, y: e.clientY, dx: panelDx, dy: panelDy };
		headDragging = true;
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
	}

	/** Clamp and apply an offset. The header drag, the agent bridge, and a
	 * window resize (while open) all land here, so they share one set of bounds. */
	function placePanel(want: PanelOffset): PanelOffset {
		if (panelEl === null) throw new Error('settings panel is not open');
		const rect = panelEl.getBoundingClientRect();
		const next = clampPanelOffset(
			want,
			{ left: rect.left - panelDx, top: rect.top - panelDy, width: rect.width },
			{ width: window.innerWidth, height: window.innerHeight }
		);
		panelDx = next.dx;
		panelDy = next.dy;
		panelEl.style.setProperty('--so-x', `${next.dx}px`);
		panelEl.style.setProperty('--so-y', `${next.dy}px`);
		return next;
	}

	function onHeadPointerMove(e: PointerEvent): void {
		if (panelDrag === null || panelEl === null) return;
		placePanel({ dx: panelDrag.dx + (e.clientX - panelDrag.x), dy: panelDrag.dy + (e.clientY - panelDrag.y) });
	}

	// Agent parity for the header move: the page-lifetime bridge in
	// panel-offset.ts reaches the panel only while it is open (Sol P1, PR #6829).
	$effect(() => {
		if (!settingsOverlay.open || panelEl === null) return;
		return attachSettingsPanel({ offset: () => ({ dx: panelDx, dy: panelDy }), place: placePanel });
	});

	/** A window shrink after the panel was parked near an edge can leave less
	 * than the promised 64 px of header on screen: re-apply the current offset
	 * through the one placement rule (issue #9321). */
	$effect(() => {
		if (!settingsOverlay.open || panelEl === null) return;
		const onResize = (): void => {
			placePanel({ dx: panelDx, dy: panelDy });
		};
		window.addEventListener('resize', onResize);
		return () => window.removeEventListener('resize', onResize);
	});

	function onHeadPointerUp(e: PointerEvent): void {
		if (panelDrag === null) return;
		panelDrag = null;
		headDragging = false;
		const el = e.currentTarget as HTMLElement;
		if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
	}

	$effect(() => {
		if (!settingsOverlay.open) {
			// The panel node is destroyed with the {#if}. Zero the offset so the
			// next open does not jump on the first drag.
			panelDx = 0;
			panelDy = 0;
			panelDrag = null;
			headDragging = false;
			return;
		}
		aiIds = [];
		aiError = null;
		applyMsg = null;
		applyOk = null;
		pendingProposal = null;
		// Untracked: _seedNumbers reads uiPrefs.pin_visible_days and the
		// drag-sensitivity settings, and a tracked read would rerun this opening
		// work (search focus, AI reset) on every slider step (Codex P2, PR #6829).
		// The pin days and sensitivity effects below keep the rows current.
		numberDraft = untrack(_seedNumbers);
		void tick().then(() => searchEl?.focus());
	});

	/** Re-read the number rows whenever the factors change, whoever changed
	 * them. The store is in a plain .ts module, so Svelte cannot track it: this
	 * subscription is what keeps the slider and its readout from showing the
	 * previous factor after `window.__mdtWheelSensitivity.set()` or `.reset()`
	 * while the panel is open (review thread r3984202679). The effect returns
	 * the unsubscribe, so closing the component leaves no listener behind. */
	$effect(() => {
		return subscribeWheelSensitivity(() => {
			numberDraft = _seedNumbers();
		});
	});

	$effect(() => {
		if (!settingsOverlay.open) return;
		const q = settingsOverlay.query.trim();
		if (q.length < 2) {
			aiIds = [];
			aiPending = false;
			return;
		}
		const seq = ++aiSeq;
		aiPending = true;
		aiError = null;
		const catalogIds = SETTINGS_CATALOG.filter((s) => settingRowVisible(s, { hideTodo, showDev })).map(
			(s) => s.id
		);
		const handle = setTimeout(() => {
			void import('$lib/settings/ai-client') // on first use (library bundle budget, PR #4014)
				.then((m) => m.aiSearchSettings(q, catalogIds))
				.then((out) => {
					if (seq !== aiSeq) return;
					aiIds = out.ids;
					aiPending = false;
				})
				.catch((err: unknown) => {
					if (seq !== aiSeq) return;
					aiPending = false;
					aiError = err instanceof Error ? err.message : String(err);
				});
		}, 280);
		return () => clearTimeout(handle);
	});

	/** Whether the press that ends in this click started, and was released,
	 * on the bare backdrop. A drag between the backdrop and the group pad or
	 * the panel, in EITHER direction, dispatches its click at their common
	 * ancestor, the backdrop itself, so the click target alone would close
	 * Settings; both ends are recorded and both must be bare backdrop
	 * (Sol P1, #6829). */
	let pressOnBackdrop = false;
	let releaseOnBackdrop = false;
	function onBackdropDown(e: PointerEvent): void {
		pressOnBackdrop = e.target === e.currentTarget;
		releaseOnBackdrop = false;
	}
	function onBackdropUp(e: PointerEvent): void {
		releaseOnBackdrop = e.target === e.currentTarget;
	}
	function onBackdrop(e: MouseEvent): void {
		const close = pressOnBackdrop && releaseOnBackdrop && e.target === e.currentTarget;
		pressOnBackdrop = false;
		releaseOnBackdrop = false;
		if (close) closeSettings();
	}

	function onSearchKeydown(e: KeyboardEvent): void {
		const len = filtered.all.length;
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			setSettingsSelectedIndex(moveSelection(settingsOverlay.selectedIndex, 1, len));
			return;
		}
		if (e.key === 'ArrowUp') {
			e.preventDefault();
			setSettingsSelectedIndex(moveSelection(settingsOverlay.selectedIndex, -1, len));
			return;
		}
		if (e.key === 'Enter') {
			e.preventDefault();
			commitSettingsSearch();
			if (selected) activateSetting(selected, e);
			return;
		}
		if (selected?.control.kind === 'boolean' || selected?.control.kind === 'enum') {
			const action = booleanKeyAction(e.key);
			if (action && selected.control.kind === 'boolean' && selected.implemented) {
				e.preventDefault();
				const cur = Boolean(readSettingValue(selected.id as AllowedSettingKey));
				const next = applyBooleanAction(cur, action);
				if (next !== null) applySettingChange(selected.id, next);
			}
		}
	}

	function onQueryInput(e: Event): void {
		setSettingsQuery((e.currentTarget as HTMLInputElement).value);
	}

	/** What each action row last reported (or why it failed), by row id, shown under the row (#12988). */
	let actionResults = $state<Record<string, string>>({});

	/** Runs an action row's safe API call (a status read or a dry run) and keeps its sentence for the row. */
	async function runActionRow(def: SettingDef): Promise<void> {
		if (def.control.kind !== 'action') return;
		const action = def.control.action;
		actionResults = { ...actionResults, [def.id]: 'Running...' };
		let message: string;
		try {
			const { runSettingAction } = await import('$lib/settings/ratings-sync-actions');
			message = await runSettingAction(action);
		} catch (exc) {
			message = `${def.label} did not run: ${exc instanceof Error ? exc.message : String(exc)}`;
		}
		actionResults = { ...actionResults, [def.id]: message };
	}

	function activateSetting(def: SettingDef, e?: Event): void {
		if (!def.implemented) return;
		if (def.control.kind === 'action') {
			e?.preventDefault();
			void runActionRow(def);
			return;
		}
		if (def.control.kind === 'link') {
			e?.preventDefault();
			closeSettings();
			void goto(def.control.href);
			return;
		}
		if (def.control.kind === 'boolean') {
			e?.preventDefault();
			const cur = Boolean(readSettingValue(def.id as AllowedSettingKey));
			applySettingChange(def.id, !cur);
		}
	}

	function onRowKeydown(def: SettingDef, index: number, e: KeyboardEvent): void {
		setSettingsSelectedIndex(index);
		if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
			e.preventDefault();
			const next = moveSelection(
				index,
				e.key === 'ArrowDown' ? 1 : -1,
				filtered.all.length
			);
			setSettingsSelectedIndex(next);
			return;
		}
		if (!def.implemented) return;
		if ((def.control.kind === 'link' || def.control.kind === 'action') && (e.key === 'Enter' || e.key === ' ')) {
			e.preventDefault();
			activateSetting(def, e);
			return;
		}
		if (def.control.kind === 'boolean') {
			const action = booleanKeyAction(e.key);
			if (action) {
				e.preventDefault();
				const cur = Boolean(readSettingValue(def.id as AllowedSettingKey));
				const next = applyBooleanAction(cur, action);
				if (next !== null) applySettingChange(def.id, next);
			}
		}
	}

	function enumValue(def: SettingDef): string {
		return String(readSettingValue(def.id as AllowedSettingKey));
	}

	function boolValue(def: SettingDef): boolean {
		return Boolean(readSettingValue(def.id as AllowedSettingKey));
	}

	/** Number-kind rows only. A boolean or a non-numeric string coming back
	 * here means the catalog and the mutator disagreed; that throws rather
	 * than rendering "NaN" into a live control. */
	function _readNumber(id: string): number {
		const raw = readSettingValue(id as AllowedSettingKey);
		if (typeof raw !== 'string') {
			throw new Error(`${id} is a number setting but read back a ${typeof raw}`);
		}
		const parsed = Number(raw);
		if (!Number.isFinite(parsed)) {
			throw new Error(`${id} is a number setting but read back ${JSON.stringify(raw)}`);
		}
		return parsed;
	}

	function _seedNumbers(): Record<string, number> {
		const seeded: Record<string, number> = {};
		for (const def of SETTINGS_CATALOG) {
			if (def.control.kind !== 'number') continue;
			seeded[def.id] = _readNumber(def.id);
		}
		return seeded;
	}

	function numberValue(def: SettingDef): number {
		return numberDraft[def.id] ?? _readNumber(def.id);
	}

	/** Whole-number settings (step 1, such as pin days) read as integers.
	 * Fractional ones keep two decimals: the sliders step in 0.05, and the
	 * shipped trackpad default is 1/3, which still has to read as a believable
	 * number rather than 0.3333333333333333. */
	function formatNumber(value: number, step: number): string {
		return value.toFixed(Number.isInteger(step) ? 0 : 2);
	}

	/** One writer for both the drag and the Reset button. The subscription
	 * above refreshes the readout, so this is the only path that writes the
	 * factor and the two cannot diverge. */
	function writeNumber(def: SettingDef, value: number): void {
		applySettingChange(def.id, String(value));
	}

	// uiPrefs is a $state store, so it is tracked: re-read the number rows
	// whenever the pin days-visible value changes (slider, Reset, or the agent bridge).
	$effect(() => {
		void uiPrefs.pin_visible_days;
		numberDraft = _seedNumbers();
	});

	// Drag sensitivity lives in a $state store, so it is tracked: re-read the
	// number rows whenever it changes (slider, Reset, or the agent bridge).
	$effect(() => {
		void dragSensitivity.base;
		void dragSensitivity.falloffPx;
		numberDraft = _seedNumbers();
	});

	function onNumberInput(def: SettingDef, e: Event): void {
		writeNumber(def, Number((e.currentTarget as HTMLInputElement).value));
	}

	function resetNumber(def: SettingDef): void {
		if (def.control.kind !== 'number') return;
		writeNumber(def, def.control.defaultValue);
	}

	/** Reload so the saved engine actually starts, through the same command the agent bridge runs. */
	function onRestartToLoadEngine(e: MouseEvent): void {
		e.stopPropagation();
		e.preventDefault();
		restartToLoadEngine();
	}

	// The watcher-folders row loads on demand (it is drawn only while settings
	// are open, and the overlay rides the library first paint; PR #4014).
	let watcherFoldersEditor: Promise<typeof import('./WatcherFoldersEditor.svelte')> | null = null;
	function loadWatcherFoldersEditor(): Promise<typeof import('./WatcherFoldersEditor.svelte')> {
		watcherFoldersEditor ??= import('./WatcherFoldersEditor.svelte');
		return watcherFoldersEditor;
	}

	let keyboardPresetUnbound: Promise<typeof import('./KeyboardPresetUnbound.svelte')> | null = null;
	function loadKeyboardPresetUnbound(): Promise<typeof import('./KeyboardPresetUnbound.svelte')> {
		keyboardPresetUnbound ??= import('./KeyboardPresetUnbound.svelte');
		return keyboardPresetUnbound;
	}

	// UIURL-03: "Open in browser" loads on demand too, for the same reason: the
	// overlay rides the library first paint, and a static import put this PR
	// 431 bytes over the "library" bundle budget (check-bundle-size.sh).
	let openInBrowserAction: Promise<typeof import('./OpenInBrowserAction.svelte')> | null = null;
	function loadOpenInBrowserAction(): Promise<typeof import('./OpenInBrowserAction.svelte')> {
		openInBrowserAction ??= import('./OpenInBrowserAction.svelte');
		return openInBrowserAction;
	}

	async function askAiApply(): Promise<void> {
		const instruction = settingsOverlay.query.trim();
		if (!instruction || applyBusy) return;
		applyBusy = true;
		applyMsg = null;
		applyOk = null;
		pendingProposal = null;
		try {
			const out = await (await import('$lib/settings/ai-client')).aiApplySetting(instruction);
			if (!out.ok || !out.proposal) {
				applyOk = false;
				applyMsg = out.error ?? 'AI refused or could not map the instruction';
				return;
			}
			pendingProposal = out.proposal;
			applyMsg = `${out.proposal.key} = ${String(out.proposal.value)} - ${out.proposal.rationale}`;
			applyOk = null;
		} catch (err) {
			applyOk = false;
			applyMsg = err instanceof Error ? err.message : String(err);
		} finally {
			applyBusy = false;
		}
	}

	function confirmProposal(accept: boolean): void {
		if (!pendingProposal) return;
		if (!accept) {
			applyOk = false;
			applyMsg = `Rejected: ${pendingProposal.key}`;
			pendingProposal = null;
			return;
		}
		try {
			applySettingChange(pendingProposal.key, pendingProposal.value);
			applyOk = true;
			applyMsg = `Applied ${pendingProposal.key} = ${String(pendingProposal.value)}`;
			pendingProposal = null;
		} catch (err) {
			applyOk = false;
			applyMsg = err instanceof Error ? err.message : String(err);
			pendingProposal = null;
		}
	}

	function groupLabel(id: SettingGroupId): string {
		return SETTING_GROUPS.find((g) => g.id === id)?.label ?? id;
	}

	// ----- setup entry point -------------------------------------------------
	/**
	 * "Run setup" lives in the ACTIONS bar rather than in the settings list,
	 * because it is not a setting: it navigates and changes engine-side state.
	 * The bar is outside `.so-body`, which is display:none until the panel
	 * expands -- an entry point you can only reach by typing a search term
	 * first is not an entry point.
	 */
	let setupBusy = $state(false);
	let setupError = $state<string | null>(null);
	/** Why setup is unreachable, or null. Only a FINAL refusal disables; an
	 * unfinished health probe leaves the button live and is resolved by the
	 * click itself. */
	const setupBlocked = $derived(runSetupBlocked());

	async function onRunSetup(): Promise<void> {
		if (setupBusy) return;
		setupBusy = true;
		setupError = null;
		try {
			setupError = await runSetup(goto, $page.url.pathname);
			if (setupError === null) closeSettings();
		} finally {
			setupBusy = false;
		}
	}
</script>

{#if settingsOverlay.open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="so-backdrop" class:so-dragging={headDragging} role="presentation" onpointerdown={onBackdropDown} onpointerup={onBackdropUp} onclick={onBackdrop}>
		<div
			bind:this={panelEl}
			class="so-panel"
			class:expanded
			role="dialog"
			aria-modal="true"
			aria-label="Settings"
			use:focusTrap={{ initialFocus: '.so-search' }}
			onclick={(e) => e.stopPropagation()}
		>
			<SettingsHeaderView
				canBack={canHistoryBack()}
				canForward={canHistoryForward()}
				onBack={() => historyBack()}
				onForward={() => historyForward()}
				bind:searchEl
				query={settingsOverlay.query}
				{onQueryInput}
				{onSearchKeydown}
				onClose={() => closeSettings()}
				onHeadPointerDown={onHeadPointerDown}
				onHeadPointerMove={onHeadPointerMove}
				onHeadPointerUp={onHeadPointerUp}
			/>

			<!-- Actions, not settings: they navigate or change engine-side state
			     rather than flipping a pref. Outside .so-body on purpose, which
			     is display:none until the panel expands. -->
			<div class="so-actions">
				<button
					type="button"
					class="so-action"
					onclick={() => void onRunSetup()}
					disabled={setupBusy || setupBlocked !== null}
					title={setupBlocked ?? RUN_SETUP_TITLE}
				>
					{setupBusy ? 'Opening setup...' : RUN_SETUP_LABEL}
				</button>
				<span class="so-action-note">
					Re-open the first-run library import wizard.
				</span>
				{#if setupError !== null}
					<span class="so-action-err" title={setupError}>{setupError}</span>
				{/if}
				{#await loadOpenInBrowserAction() then { default: OpenInBrowserAction }}
					<OpenInBrowserAction compact />
				{:catch err}
					<span class="so-action-err" title={String(err)}>Open in browser failed to load: {String(err)}</span>
				{/await}
			</div>

			<div class="so-body" class:expanded>
				<aside class="so-lhs" aria-label="Setting categories">
					<button
						type="button"
						class="so-cat"
						class:on={settingsOverlay.group === null}
						onclick={() => setSettingsGroup(null)}
					>
						All
					</button>
					{#each SETTING_GROUPS as g (g.id)}
						{#if groupsShown.includes(g.id) || settingsOverlay.group === g.id}
							<button
								type="button"
								class="so-cat"
								class:on={settingsOverlay.group === g.id}
								onclick={() => setSettingsGroup(g.id)}
							>
								{g.label}
							</button>
						{/if}
					{/each}
				</aside>

				<section class="so-rhs" aria-label="Settings results">
					{#if filtered.all.length === 0}
						<p class="so-empty">No settings match. Try another word (theme, sync, density).</p>
					{:else}
						<ul class="so-list">
							{#each filtered.all as def, i (def.id)}
								{@const isAi = aiIdSet.has(def.id) && !filtered.keyword.some((k) => k.id === def.id)}
								{@const isSel = i === settingsOverlay.selectedIndex}
								<!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
								<li
									class="so-row"
									class:todo={!def.implemented}
									class:ai={isAi}
									class:sel={isSel}
									class:link-row={def.implemented && def.control.kind === 'link'}
									role="option"
									aria-selected={isSel}
									aria-disabled={!def.implemented}
									tabindex="0"
									onmouseenter={() => {
										setSettingsSelectedIndex(i);
									}}
									onfocus={() => {
										setSettingsSelectedIndex(i);
									}}
									onkeydown={(e) => onRowKeydown(def, i, e)}
									onclick={() => activateSetting(def)}
								>
									<div class="so-row-main">
										<div class="so-meta">
											<span class="so-label">{def.label}</span>
											<span class="so-group">{groupLabel(def.group)}</span>
										</div>
										<div class="so-control" onclick={(e) => e.stopPropagation()}>
											{#if !def.implemented}
												<span class="so-inert" title={INERT}>todo</span>
											{:else if def.control.kind === 'boolean'}
												<label class="so-toggle">
													<input
														type="checkbox"
														aria-label={def.label}
														checked={boolValue(def)}
														onchange={() =>
															applySettingChange(def.id, !boolValue(def))}
													/>
													<span>{boolValue(def) ? 'On' : 'Off'}</span>
												</label>
											{:else if def.control.kind === 'enum'}
												<div class="so-enum">
													<select
														aria-label={def.label}
														value={enumValue(def)}
														onchange={(e) =>
															applySettingChange(
																def.id,
																(e.currentTarget as HTMLSelectElement).value
															)}
													>
														{#each def.control.options as opt (opt.value)}
															<option value={opt.value}>{opt.label}</option>
														{/each}
													</select>
													{#if def.id === 'perf_tier'}
														<span data-testid="perf-tier-on-now">{tierOnNowLabel()}</span>
													{/if}
													{#if def.id === 'keyboard_preset'}
														{#await loadKeyboardPresetUnbound() then { default: KeyboardPresetUnbound }}
															<KeyboardPresetUnbound {def} />
														{/await}
													{/if}
												</div>
											{:else if def.control.kind === 'multi_bool'}
												<div class="so-multi" role="group" aria-label={def.label}>
													{#each def.control.keys as key (key.id)}
														<label class="so-chip" title={key.title}>
															<input
																type="checkbox"
																checked={uiPrefs.auto_sync[key.id as AutoSyncDestination]}
																onchange={() =>
																	setAutoSyncDestination(
																		key.id as AutoSyncDestination,
																		!uiPrefs.auto_sync[key.id as AutoSyncDestination]
																	)}
															/>
															{key.label}
														</label>
													{/each}
												</div>
											{:else if def.control.kind === 'link'}
												<a
													href={def.control.href}
													aria-label={`Open ${def.label}`}
													onclick={() => closeSettings()}
												>
													Open
												</a>
											{:else if def.control.kind === 'action'}
												<button type="button" aria-label={`Run ${def.label}`} onclick={() => void runActionRow(def)}>Run</button>
											{:else if def.control.kind === 'number'}
												<div class="so-number">
													<input
														type="range"
														aria-label={def.label}
														min={def.control.min}
														max={def.control.max}
														step={def.control.step}
														value={numberValue(def)}
														oninput={(e) => onNumberInput(def, e)}
													/>
													<span class="so-number-out" title={`Current value of ${def.label}`}>
														{formatNumber(numberValue(def), def.control.step)}{def.control.unit}
													</span>
													<button
														type="button"
														class="so-number-reset"
														title={`Back to the default ${formatNumber(def.control.defaultValue, def.control.step)}${def.control.unit}`}
														onclick={() => resetNumber(def)}
													>
														Reset
													</button>
												</div>
											{:else if def.control.kind === 'path_lines'}
												{#await loadWatcherFoldersEditor() then { default: WatcherFoldersEditor }}
													<WatcherFoldersEditor {def} />
												{:catch err}
													<span class="so-path-lines-msg" title={String(err)}>Watched folders failed to load: {String(err)}</span>
												{/await}
											{/if}
										</div>
									</div>
									{#if def.preview !== undefined}
										<!-- Static, not a lazy import: a pending or failed chunk rendered NOTHING, silently,
										     and on a cold /performance load the dev server took over 8 s to serve it (Mon 5 Oct 2026).
										     Full card width, below the label and control (the maintainer, Tue 6 Oct 2026). -->
										<SettingPreview kind={def.preview} label={`${def.label} preview`} />
									{/if}
									{#if def.tester === 'drag'}
										<!-- Lazy: this overlay is mounted by the root layout, so a static import put the
										     mixer Knob and drag code into the library's initial load (bundle budget).
										     Loading and failure both render text, never nothing. -->
										{#await import('./DragSensitivityTester.svelte')}
											<p class="so-detail" data-testid="drag-tester-loading">Loading the drag tester</p>
										{:then { default: DragSensitivityTester }}
											<DragSensitivityTester
												onresetsensitivity={() =>
													setDragSensitivity({ base: DRAG_DEFAULTS.base, falloff_px: DRAG_DEFAULTS.falloffPx })}
											/>
										{:catch err}
											<p class="so-detail" data-testid="drag-tester-failed" title="The test knobs and faders could not be loaded">
												The drag tester failed to load: {err instanceof Error ? err.message : String(err)}
											</p>
										{/await}
									{/if}
									{#if settingUnavailable(def.id) !== null}
										<p class="so-detail so-unavailable" role="alert">{settingUnavailable(def.id)}</p>
									{/if}
									<p class="so-detail">{def.detail}</p>
									{#if actionResults[def.id] !== undefined}
										<p class="so-detail" role="status" data-testid="settings-action-result">{actionResults[def.id]}</p>
									{/if}
									{#if def.id === 'audio_engine' && showEngineRestart}
										<button
											type="button"
											class="so-engine-restart"
											onclick={onRestartToLoadEngine}
										>
											Restart to load engine
										</button>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}

					<SettingsAiAskView
						{aiPending}
						{aiError}
						{applyBusy}
						query={settingsOverlay.query}
						{applyMsg}
						{applyOk}
						{pendingProposal}
						askAiApply={() => void askAiApply()}
						{confirmProposal}
					/>
				</section>
			</div>
		</div>
		<!-- Above the backdrop, so a group of drag-tester controls (or mixer
		     dials) has a usable pad while settings is open on any route. -->
		<GroupPad inSettings />
	</div>
{/if}

<style>
	.so-backdrop {
		position: fixed;
		inset: 0;
		z-index: 400;
		display: flex;
		align-items: flex-start;
		justify-content: center;
		padding: 12vh 1rem 2rem;
		/* No dim: the window is dragged aside specifically so the page behind
		   it stays visible. The layer still catches an outside click to close. */
		background: transparent;
		animation: so-fade 120ms ease-out;
	}
	.so-backdrop.so-dragging,
	.so-backdrop.so-dragging .so-panel {
		cursor: grabbing;
	}
	.so-panel {
		width: min(720px, 96vw);
		max-height: 76vh;
		display: flex;
		flex-direction: column;
		background: var(--surface-raised, #222a38);
		border: 1px solid var(--border, #1c222c);
		border-radius: 12px;
		box-shadow: 0 18px 50px rgba(0, 0, 0, 0.45);
		overflow: hidden;
		transform: scale(0.98);
		translate: var(--so-x, 0px) var(--so-y, 0px);
		animation: so-pop 140ms ease-out forwards;
	}
	.so-panel.expanded {
		width: min(920px, 96vw);
		max-height: 82vh;
	}
	.so-actions {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
		padding: 8px 12px;
		border-bottom: 1px solid var(--border, #1c222c);
	}
	.so-action {
		padding: 6px 12px;
		border-radius: 8px;
		border: 1px solid var(--border, #1c222c);
		background: var(--surface-hover, #1a212c);
		color: var(--fg, #e6e9ef);
		cursor: pointer;
		font-size: 0.85rem;
	}
	.so-action:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.so-action-note {
		font-size: 0.78rem;
		color: var(--muted, #9aa4b2);
	}
	.so-action-err {
		font-size: 0.78rem;
		color: var(--danger, #ff5a5a);
	}
	.so-body {
		display: none;
		min-height: 0;
		flex: 1;
	}
	.so-body.expanded {
		display: grid;
		grid-template-columns: 180px 1fr;
	}
	.so-lhs {
		border-right: 1px solid var(--border, #1c222c);
		padding: 8px;
		overflow: auto;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.so-cat {
		text-align: left;
		padding: 7px 10px;
		border: none;
		border-radius: 8px;
		background: transparent;
		color: var(--muted, #9aa4b2);
		cursor: pointer;
		font-size: 0.85rem;
	}
	.so-cat.on,
	.so-cat:hover {
		background: var(--surface-hover, #1a212c);
		color: var(--fg, #e6e9ef);
	}
	.so-rhs {
		padding: 8px 10px 12px;
		overflow: auto;
		min-height: 0;
	}
	.so-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.so-row {
		padding: 10px 12px;
		border-radius: 10px;
		border: 1px solid transparent;
		background: var(--surface, #121720);
		cursor: default;
		animation: so-row-in 160ms ease-out;
	}
	.so-row.sel {
		background: var(--surface-hover, #1a212c);
		border-color: var(--border, #1c222c);
	}
	.so-row.ai {
		border-color: rgba(80, 200, 120, 0.45);
		box-shadow: inset 0 0 0 1px rgba(80, 200, 120, 0.2);
	}
	.so-row.todo {
		opacity: 0.55;
	}
	.so-row.link-row {
		cursor: pointer;
	}
	.so-row-main {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
	}
	.so-meta {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.so-label {
		font-size: 0.95rem;
		font-weight: 560;
	}
	.so-group {
		font-size: 0.72rem;
		color: var(--muted, #9aa4b2);
	}
	.so-unavailable {
		color: var(--danger, #f87171);
	}
	.so-detail {
		margin: 8px 0 0;
		font-size: 0.8rem;
		color: var(--muted, #9aa4b2);
		line-height: 1.35;
	}
	.so-engine-restart {
		display: inline-block;
		margin-top: 8px;
		padding: 4px 10px;
		border-radius: 8px;
		border: 1px solid var(--accent, #ffb43a);
		background: var(--surface-hover, #1a212c);
		color: var(--fg, #e6e9ef);
		cursor: pointer;
		font-size: 0.8rem;
		font-weight: 560;
	}
	.so-engine-restart:hover {
		background: var(--accent, #ffb43a);
		color: var(--bg, #0b0d11);
	}
	.so-control select,
	.so-toggle,
	.so-chip {
		font-size: 0.82rem;
	}
	.so-control select {
		background: var(--bg, #0b0d11);
		color: var(--fg, #e6e9ef);
		border: 1px solid var(--border, #1c222c);
		border-radius: 8px;
		padding: 4px 8px;
	}
	.so-toggle,
	.so-chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		cursor: pointer;
		color: var(--fg, #e6e9ef);
	}
	.so-multi {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	.so-enum,
	.so-number {
		display: inline-flex;
		align-items: center;
		gap: 8px;
	}
	.so-number input[type='range'] {
		width: 130px;
		accent-color: var(--accent, #ffb43a);
	}
	.so-number-out {
		min-width: 3.4rem;
		font-variant-numeric: tabular-nums;
		color: var(--fg, #e6e9ef);
	}
	.so-number-reset {
		padding: 3px 8px;
		border-radius: 8px;
		border: 1px solid var(--border, #1c222c);
		background: var(--surface, #121720);
		color: var(--muted, #9aa4b2);
		cursor: pointer;
		font-size: 0.75rem;
	}
	.so-inert {
		font-size: 0.75rem;
		color: var(--muted, #9aa4b2);
		border: 1px solid var(--border, #1c222c);
		border-radius: 999px;
		padding: 2px 8px;
	}
	.so-empty {
		color: var(--muted, #9aa4b2);
		font-size: 0.85rem;
		margin: 8px 4px;
	}
	@keyframes so-fade {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}
	@keyframes so-pop {
		to {
			transform: scale(1);
		}
	}
	@keyframes so-row-in {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	/* A11Y-11811: reduced motion (OS or Appearance); mirrors app.css. */
	@media (prefers-reduced-motion: reduce) {
		:where(*) {
			animation-duration: 0.01ms;
			animation-iteration-count: 1;
			transition-duration: 0.01ms;
		}
	}
	:global(html[data-reduce-motion]) :where(*) {
		animation-duration: 0.01ms;
		animation-iteration-count: 1;
		transition-duration: 0.01ms;
	}

</style>
