<script lang="ts">
	/**
	 * Warp-style searchable settings overlay.
	 * Opens like a command palette, expands for results; RHS rows ARE the settings.
	 */
	import { tick } from 'svelte';
	import { goto } from '$app/navigation';
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
	import { applySettingChange, readSettingValue, type AllowedSettingKey } from '$lib/settings/apply';
	import { aiApplySetting, aiSearchSettings } from '$lib/settings/ai-client';
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
	import { filterSettings, visibleGroups } from '$lib/settings/search';
	import { subscribeWheelSensitivity } from '$lib/rb/wheel-adjust';
	import {
		formatWatcherFolderLines,
		parseWatcherFolderLines,
		validateWatcherFoldersExist
	} from '$lib/rb/library-watcher-folders';
	import {
		setAutoSyncDestination,
		setLibraryWatcherFolders,
		uiPrefs,
		type AutoSyncDestination
	} from '$lib/rb/prefs.svelte';

	const INERT = 'not implemented - see PARITY-TODO';

	let searchEl = $state<HTMLInputElement | null>(null);
	let aiIds = $state<string[]>([]);
	let aiPending = $state(false);
	let aiError = $state<string | null>(null);
	let aiSeq = 0;
	let focusedId = $state<string | null>(null);
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
	let pathLinesDraft = $state<Record<string, string>>({});
	let pathLinesBusy = $state(false);
	let pathLinesMsg = $state<string | null>(null);

	const hideTodo = $derived(uiPrefs.hide_todo_settings);
	const filterOpts = $derived({
		hideTodo,
		group: settingsOverlay.group as SettingGroupId | null,
		aiIds
	});
	const filtered = $derived(filterSettings(settingsOverlay.query, filterOpts));
	const groupsShown = $derived(visibleGroups(settingsOverlay.query, { hideTodo, aiIds }));
	const selected = $derived(
		filtered.all[clampIndex(settingsOverlay.selectedIndex, filtered.all.length)] ?? null
	);
	const aiIdSet = $derived(new Set(aiIds));
	const expanded = $derived(settingsOverlay.query.trim().length > 0 || filtered.all.length > 0);

	$effect(() => {
		if (!settingsOverlay.open) return;
		aiIds = [];
		aiError = null;
		applyMsg = null;
		applyOk = null;
		pendingProposal = null;
		numberDraft = _seedNumbers();
		pathLinesDraft = _seedPathLines();
		pathLinesMsg = null;
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
		const catalogIds = SETTINGS_CATALOG.filter((s) => !hideTodo || s.implemented).map(
			(s) => s.id
		);
		const handle = setTimeout(() => {
			void aiSearchSettings(q, catalogIds)
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

	function onBackdrop(e: MouseEvent): void {
		if (e.target === e.currentTarget) closeSettings();
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

	function activateSetting(def: SettingDef, e?: Event): void {
		if (!def.implemented) return;
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
		focusedId = def.id;
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
		if (def.control.kind === 'link' && (e.key === 'Enter' || e.key === ' ')) {
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

	/** Two decimals: the sliders step in 0.05, and the shipped trackpad default
	 * is 1/3, which no step lands on exactly but which still has to read as a
	 * believable number rather than 0.3333333333333333. */
	function formatNumber(value: number): string {
		return value.toFixed(2);
	}

	/** One writer for both the drag and the Reset button. The subscription
	 * above refreshes the readout, so this is the only path that writes the
	 * factor and the two cannot diverge. */
	function writeNumber(def: SettingDef, value: number): void {
		applySettingChange(def.id, String(value));
	}

	function onNumberInput(def: SettingDef, e: Event): void {
		writeNumber(def, Number((e.currentTarget as HTMLInputElement).value));
	}

	function resetNumber(def: SettingDef): void {
		if (def.control.kind !== 'number') return;
		writeNumber(def, def.control.defaultValue);
	}

	function _seedPathLines(): Record<string, string> {
		return { library_watcher_folders: formatWatcherFolderLines(uiPrefs.library_watcher_folders) };
	}

	function pathLinesValue(def: SettingDef): string {
		return pathLinesDraft[def.id] ?? formatWatcherFolderLines(uiPrefs.library_watcher_folders);
	}

	async function applyPathLines(def: SettingDef): Promise<void> {
		if (def.control.kind !== 'path_lines') return;
		pathLinesBusy = true;
		pathLinesMsg = null;
		try {
			const paths = parseWatcherFolderLines(pathLinesValue(def));
			await validateWatcherFoldersExist(paths);
			setLibraryWatcherFolders(paths);
			pathLinesDraft = { ...pathLinesDraft, [def.id]: formatWatcherFolderLines(paths) };
			pathLinesMsg = paths.length === 0 ? 'Cleared watcher folders.' : `Saved ${paths.length} folder(s).`;
		} catch (err) {
			pathLinesMsg = err instanceof Error ? err.message : String(err);
		} finally {
			pathLinesBusy = false;
		}
	}

	async function askAiApply(): Promise<void> {
		const instruction = settingsOverlay.query.trim();
		if (!instruction || applyBusy) return;
		applyBusy = true;
		applyMsg = null;
		applyOk = null;
		pendingProposal = null;
		try {
			const out = await aiApplySetting(instruction);
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
			setupError = await runSetup(goto);
			if (setupError === null) closeSettings();
		} finally {
			setupBusy = false;
		}
	}
</script>

{#if settingsOverlay.open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="so-backdrop" role="presentation" onclick={onBackdrop}>
		<div
			class="so-panel"
			class:expanded
			role="dialog"
			aria-modal="true"
			aria-label="Settings"
			onclick={(e) => e.stopPropagation()}
		>
			<header class="so-head">
				<div class="so-nav">
					<button
						type="button"
						class="so-hist"
						disabled={!canHistoryBack()}
						onclick={() => historyBack()}
						title="Back in settings navigation"
						aria-label="Back"
					>
						&lt;
					</button>
					<button
						type="button"
						class="so-hist"
						disabled={!canHistoryForward()}
						onclick={() => historyForward()}
						title="Forward in settings navigation"
						aria-label="Forward"
					>
						&gt;
					</button>
				</div>
				<input
					bind:this={searchEl}
					class="so-search"
					type="search"
					placeholder="Search settings..."
					spellcheck="false"
					autocomplete="off"
					value={settingsOverlay.query}
					oninput={onQueryInput}
					onkeydown={onSearchKeydown}
					aria-label="Search settings"
				/>
				<button
					type="button"
					class="so-close"
					onclick={() => closeSettings()}
					aria-label="Close settings"
					title="Close (Esc)"
				>
					Esc
				</button>
			</header>

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
								{@const showDetail = focusedId === def.id || isSel}
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
									title={def.title}
									onmouseenter={() => {
										focusedId = def.id;
										setSettingsSelectedIndex(i);
									}}
									onfocus={() => {
										focusedId = def.id;
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
												<label class="so-toggle" title={def.title}>
													<input
														type="checkbox"
														checked={boolValue(def)}
														onchange={() =>
															applySettingChange(def.id, !boolValue(def))}
													/>
													<span>{boolValue(def) ? 'On' : 'Off'}</span>
												</label>
											{:else if def.control.kind === 'enum'}
												<select
													title={def.title}
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
											{:else if def.control.kind === 'multi_bool'}
												<div class="so-multi" title={def.title}>
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
													title={def.title}
													onclick={() => closeSettings()}
												>
													Open
												</a>
											{:else if def.control.kind === 'number'}
												<div class="so-number" title={def.title}>
													<input
														type="range"
														aria-label={def.label}
														min={def.control.min}
														max={def.control.max}
														step={def.control.step}
														value={numberValue(def)}
														oninput={(e) => onNumberInput(def, e)}
													/>
													<span class="so-number-out">
														{formatNumber(numberValue(def))}{def.control.unit}
													</span>
													<button
														type="button"
														class="so-number-reset"
														title={`Back to the default ${formatNumber(def.control.defaultValue)}${def.control.unit}`}
														onclick={() => resetNumber(def)}
													>
														Reset
													</button>
												</div>
											{:else if def.control.kind === 'path_lines'}
												<div class="so-path-lines" title={def.title}>
													<p class="so-v2-notice" title={def.control.v2Notice}>
														{def.control.v2Notice}
													</p>
													<textarea
														aria-label={def.label}
														rows={3}
														value={pathLinesValue(def)}
														oninput={(e) => {
															pathLinesDraft = {
																...pathLinesDraft,
																[def.id]: (e.currentTarget as HTMLTextAreaElement).value
															};
														}}
													></textarea>
													<button
														type="button"
														disabled={pathLinesBusy}
														title="Validate paths exist on disk, then save"
														onclick={() => void applyPathLines(def)}
													>
														{pathLinesBusy ? 'Saving…' : 'Apply'}
													</button>
													{#if pathLinesMsg && def.id === 'library_watcher_folders'}
														<span class="so-path-lines-msg" title={pathLinesMsg}>{pathLinesMsg}</span>
													{/if}
												</div>
											{/if}
										</div>
									</div>
									{#if showDetail}
										<p class="so-detail">{def.detail}</p>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}

					{#if aiPending}
						<p class="so-ai-status">AI matching...</p>
					{:else if aiError}
						<p class="so-ai-status err" title={aiError}>AI search unavailable</p>
					{/if}

					<div class="so-ai-ask">
						<button
							type="button"
							class="so-ask-btn"
							disabled={applyBusy || settingsOverlay.query.trim().length < 2}
							onclick={() => void askAiApply()}
							title="Ask the fast model to propose one allowlisted setting change"
						>
							{applyBusy ? 'Asking AI...' : 'Ask AI to change setting for you'}
						</button>
						{#if applyMsg}
							<p class="so-apply-msg" class:ok={applyOk === true} class:bad={applyOk === false}>
								{#if applyOk === true}✅{:else if applyOk === false}✗{/if}
								{applyMsg}
							</p>
						{/if}
						{#if pendingProposal}
							<div class="so-confirm">
								<button type="button" onclick={() => confirmProposal(true)}>✅ Apply</button>
								<button type="button" onclick={() => confirmProposal(false)}>✗ Reject</button>
							</div>
						{/if}
					</div>
				</section>
			</div>
		</div>
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
		background: var(--overlay, rgba(0, 0, 0, 0.72));
		animation: so-fade 120ms ease-out;
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
		animation: so-pop 140ms ease-out forwards;
	}
	.so-panel.expanded {
		width: min(920px, 96vw);
		max-height: 82vh;
	}
	.so-head {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 12px 12px 10px;
		border-bottom: 1px solid var(--border, #1c222c);
	}
	.so-nav {
		display: inline-flex;
		gap: 2px;
	}
	.so-hist,
	.so-close {
		height: 32px;
		min-width: 32px;
		padding: 0 8px;
		border-radius: 8px;
		border: 1px solid var(--border, #1c222c);
		background: var(--surface, #121720);
		color: var(--fg, #e6e9ef);
		cursor: pointer;
	}
	.so-hist:disabled {
		opacity: 0.35;
		cursor: default;
	}
	.so-search {
		flex: 1;
		height: 40px;
		padding: 0 14px;
		border-radius: 10px;
		border: 1px solid var(--border, #1c222c);
		background: var(--bg, #0b0d11);
		color: var(--fg, #e6e9ef);
		font-size: 1.05rem;
		outline: none;
	}
	.so-search:focus {
		border-color: var(--accent, #ffb43a);
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
	.so-detail {
		margin: 8px 0 0;
		font-size: 0.8rem;
		color: var(--muted, #9aa4b2);
		line-height: 1.35;
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
	.so-empty,
	.so-ai-status {
		color: var(--muted, #9aa4b2);
		font-size: 0.85rem;
		margin: 8px 4px;
	}
	.so-ai-status.err {
		color: var(--danger, #ff5a5a);
	}
	.so-ai-ask {
		margin-top: 12px;
		padding-top: 10px;
		border-top: 1px solid var(--border, #1c222c);
	}
	.so-ask-btn {
		width: 100%;
		padding: 10px 12px;
		border-radius: 10px;
		border: 1px solid var(--border, #1c222c);
		background: var(--surface-hover, #1a212c);
		color: var(--fg, #e6e9ef);
		cursor: pointer;
		font-size: 0.9rem;
	}
	.so-ask-btn:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.so-apply-msg {
		margin: 8px 2px 0;
		font-size: 0.82rem;
		color: var(--muted, #9aa4b2);
	}
	.so-apply-msg.ok {
		color: #5dce8a;
	}
	.so-apply-msg.bad {
		color: var(--danger, #ff5a5a);
	}
	.so-confirm {
		display: flex;
		gap: 8px;
		margin-top: 8px;
	}
	.so-confirm button {
		padding: 6px 12px;
		border-radius: 8px;
		border: 1px solid var(--border, #1c222c);
		background: var(--surface, #121720);
		color: var(--fg, #e6e9ef);
		cursor: pointer;
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
</style>
