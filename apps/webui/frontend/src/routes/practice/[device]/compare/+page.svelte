<script lang="ts">
	/**
	 * /practice/<device>/compare: replica vs reference at matching mm scale (ADR-0137, #9406).
	 * Agent parity: `window.musicDjToolsPracticeCompare` (typed commands + query + report) and
	 * `opendj practice <device> --compare [--ref ...] [--seed N] [--scale S] [--trials | --answer ...]`.
	 * No practice MIDI session.
	 */
	import { page } from '$app/state';
	import DeviceReplica from '$lib/components/rb/practice/DeviceReplica.svelte';
	import {
		createPracticeCompareIpc,
		installPracticeCompareIpc
	} from '$lib/components/rb/practice/practice-compare-ipc';
	import {
		applyCompareCommand,
		blindTrials as buildBlindTrials,
		buildCompareReport,
		compareQuery,
		compareScale,
		newCompareSession,
		refSource,
		type CompareCommand,
		type CompareMode,
		type CompareSession,
		type CompareSource
	} from '$lib/components/rb/practice/practice-compare';
	import { fetchPanelLayout, practiceMapFor } from '$lib/components/rb/practice/practice-devices';
	import type { PanelLayout } from '$lib/components/rb/practice/practice-bindings';

	const device = $derived(page.params.device ?? '');
	const refParam = $derived(page.url.searchParams.get('ref'));
	const seedNum = $derived.by(() => {
		const raw = page.url.searchParams.get('seed');
		if (raw === null || raw === '') return Date.now() & 0xffffffff;
		const n = Number(raw);
		return Number.isFinite(n) ? n >>> 0 : Date.now() & 0xffffffff;
	});
	const scaleOverride = $derived.by(() => {
		const raw = page.url.searchParams.get('scale');
		if (raw === null || raw === '') return null;
		const n = Number(raw);
		return Number.isFinite(n) && n > 0 ? n : null;
	});

	let layout = $state<PanelLayout | null>(null);
	let loadError = $state<string | null>(null);

	let refFromPicker = $state<string | null>(null);
	let refLabel = $state<string>('');
	let imgNatural = $state<{ width: number; height: number } | null>(null);
	let imgError = $state(false);

	let session = $state<CompareSession | null>(null);
	let choiceLetter = $state<'A' | 'B' | null>(null);
	let ratingVal = $state<string | null>(null);

	const effectiveRefUrl = $derived(
		refFromPicker ?? (refParam?.startsWith('file:') ? null : refParam)
	);

	const refMeta = $derived(refSource(refParam));
	const scaleBundle = $derived(
		layout === null
			? null
			: compareScale(layout.panel, imgNatural, scaleOverride === null ? undefined : scaleOverride)
	);

	const pickerHint = $derived(
		refMeta.kind === 'file' && refFromPicker === null
			? `Pick ${refMeta.fileName ?? 'image'} to load the reference`
			: imgError && refFromPicker === null
				? 'Image failed to load; pick a file or use an http(s) / data URL'
				: null
	);

	function onImgLoad(e: Event): void {
		const el = e.currentTarget as HTMLImageElement;
		imgNatural = { width: el.naturalWidth, height: el.naturalHeight };
		imgError = false;
	}

	function onImgError(): void {
		imgError = true;
		imgNatural = null;
	}

	function onPickFile(e: Event): void {
		const input = e.currentTarget as HTMLInputElement;
		const f = input.files?.[0];
		if (!f) return;
		if (refFromPicker !== null) URL.revokeObjectURL(refFromPicker);
		refFromPicker = URL.createObjectURL(f);
		refLabel = f.name;
		imgNatural = null;
		imgError = false;
	}

	$effect(() => {
		const wanted = device;
		void seedNum;
		layout = null;
		loadError = null;
		session = null;
		choiceLetter = null;
		ratingVal = null;
		let live = true;
		fetchPanelLayout(wanted).then(
			(loaded) => {
				if (!live) return;
				layout = loaded;
				session = newCompareSession(buildBlindTrials(loaded.panel, seedNum, 3, 2));
			},
			(err: unknown) => {
				if (live) loadError = String(err);
			}
		);
		return () => {
			live = false;
		};
	});

	function refForReport(): string {
		if (refLabel !== '') return refLabel;
		if (refParam) return refParam;
		return '';
	}

	const uiMode = $derived((session?.mode ?? 'split') as CompareMode);
	const opacityPct = $derived(session?.opacity_pct ?? 50);
	const blindTrialsState = $derived(session?.trials ?? []);
	const blindIndex = $derived(session?.index ?? 0);

	function ownerQuery() {
		if (layout === null || session === null) {
			throw new Error('compare session not ready');
		}
		return Object.freeze(
			compareQuery(session, {
				device: layout.device,
				seed: seedNum,
				ref: refForReport(),
				pxPerMm: scaleBundle?.pxPerMm ?? null
			})
		);
	}

	function ownerReport() {
		if (layout === null || session === null || scaleBundle === null) {
			throw new Error('compare report not ready');
		}
		return buildCompareReport({
			device: layout.device,
			ref: refForReport(),
			seed: seedNum,
			pxPerMm: scaleBundle.pxPerMm,
			trials: session.trials,
			recordedAt: new Date().toISOString()
		});
	}

	function dispatch(cmd: CompareCommand) {
		if (session === null) throw new Error('no compare session');
		try {
			session = applyCompareCommand(session, cmd);
			return ownerQuery();
		} catch (err: unknown) {
			const msg = err instanceof Error ? err.message : String(err);
			session = { ...session, last_error: msg };
			throw err;
		}
	}

	function downloadJson(): void {
		if (layout === null || scaleBundle === null || session === null) return;
		const report = ownerReport();
		const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
		const a = document.createElement('a');
		a.href = URL.createObjectURL(blob);
		a.download = `practice-compare-${layout.device}-${seedNum}.json`;
		a.click();
		URL.revokeObjectURL(a.href);
	}

	function nextBlindTrial(): void {
		if (layout === null || session === null) return;
		const t = blindTrialsState[blindIndex];
		if (t === undefined || choiceLetter === null || ratingVal === null) return;
		const r = Number(ratingVal);
		if (!Number.isInteger(r) || r < 1 || r > 5) return;
		dispatch({ type: 'answer', letter: choiceLetter, rating: r });
		choiceLetter = null;
		ratingVal = null;
	}

	$effect(() => {
		if (session === null || layout === null) return;
		const ipc = createPracticeCompareIpc({
			dispatch,
			query: ownerQuery,
			report: ownerReport
		});
		return installPracticeCompareIpc(ipc);
	});

	const answeredCount = $derived(blindTrialsState.filter((t) => t.rating !== null).length);

	const currentTrial = $derived(blindTrialsState[blindIndex] ?? null);

	function slotSource(which: 0 | 1): CompareSource | null {
		const t = currentTrial;
		if (t === null) return null;
		return t.order[which]!;
	}
</script>

<svelte:head><title>Practice compare - {device}</title></svelte:head>

<main class="compare" data-testid="practice-compare-page">
	{#if loadError !== null}
		<p class="error" role="alert">Could not load practice layout for {device}: {loadError}</p>
	{:else if layout === null}
		<p>Loading layout...</p>
	{:else}
		<header>
			<h1>Compare: {layout.display_name}</h1>
			<p class="note" title="Panel width from layout drives pixels per mm">
				Replica and reference use the same scale (panel width {layout.panel.w} mm).
			</p>
		</header>

		<div class="toolbar">
			<div class="modes" title="How to view replica and reference">
				<button
					type="button"
					class:active={uiMode === 'split'}
					title="Side-by-side view"
					onclick={() => dispatch({ type: 'set_mode', mode: 'split' })}>Side by side</button
				>
				<button
					type="button"
					class:active={uiMode === 'overlay'}
					title="Overlay reference on replica with adjustable opacity"
					onclick={() => dispatch({ type: 'set_mode', mode: 'overlay' })}>Overlay</button
				>
				<button
					type="button"
					class:active={uiMode === 'blind'}
					title="Blind A/B crops of the same region"
					onclick={() => dispatch({ type: 'set_mode', mode: 'blind' })}>Blind</button
				>
			</div>
			<label class="file-row">
				<span title="Load a local image; refs are not committed">Reference file</span>
				<input
					type="file"
					accept="image/*"
					title="Pick a local reference image"
					onchange={onPickFile}
				/>
			</label>
			{#if uiMode === 'overlay'}
				<label class="opacity" title="Reference layer opacity in overlay mode">
					Opacity
					<input
						type="range"
						min="0"
						max="100"
						value={opacityPct}
						title="Drag to set reference layer opacity (0 to 100 percent)"
						oninput={(e) => dispatch({ type: 'set_opacity', pct: Number(e.currentTarget.value) })}
					/>
					<span title="Reference opacity as a percentage">{opacityPct}%</span>
				</label>
			{/if}
			<span
				class="readout"
				title="Calculated pixels per millimetre from reference width and panel width"
				data-testid="compare-px-per-mm"
			>
				px per mm: {scaleBundle === null ? '-' : scaleBundle.pxPerMm.toFixed(2)}
			</span>
		</div>

		{#if pickerHint !== null}
			<p class="hint" role="status">{pickerHint}</p>
		{/if}
		{#if session !== null && session.last_error !== null}
			<p
				class="hint compare-err"
				role="status"
				title="Last rejected compare command or validation error"
			>
				{session.last_error}
			</p>
		{/if}

		{#if scaleBundle !== null}
			{#if uiMode === 'split'}
				<div class="split" title="Scroll horizontally if the panel is wider than the viewport">
					<div
						class="lane"
						data-testid="compare-replica"
						data-px-per-mm={String(scaleBundle.pxPerMm)}
						style:width="{scaleBundle.replica.width}px"
					>
						<h2 class="lane-label" title="Schematic replica layer">Replica</h2>
						<div class="replica-wrap">
							<DeviceReplica {layout} map={practiceMapFor(layout)} disablePointerEvents={true} />
						</div>
					</div>
					<div
						class="lane"
						data-testid="compare-reference"
						data-px-per-mm={String(scaleBundle.pxPerMm)}
						style:width="{scaleBundle.replica.width}px"
					>
						<h2 class="lane-label" title="Reference photograph or scan">Reference</h2>
						{#if effectiveRefUrl}
							<img
								class="ref-img"
								src={effectiveRefUrl}
								alt=""
								onload={onImgLoad}
								onerror={onImgError}
								data-testid="compare-reference-img"
								style:width="{scaleBundle.reference?.width ?? scaleBundle.replica.width}px"
								style:height="{scaleBundle.reference?.height ?? scaleBundle.replica.height}px"
							/>
						{:else}
							<p class="placeholder" title="No reference URL loaded">No reference image yet.</p>
						{/if}
					</div>
				</div>
			{:else if uiMode === 'overlay'}
				<div
					class="overlay-stack"
					style:width="{scaleBundle.replica.width}px"
					style:height="{scaleBundle.replica.height}px"
					title="Reference drawn over replica; adjust opacity with the slider"
				>
					<div class="replica-wrap overlay-bottom">
						<DeviceReplica {layout} map={practiceMapFor(layout)} disablePointerEvents={true} />
					</div>
					{#if effectiveRefUrl}
						<img
							class="ref-overlay"
							src={effectiveRefUrl}
							alt=""
							onload={onImgLoad}
							onerror={onImgError}
							style:width="{scaleBundle.reference?.width ?? scaleBundle.replica.width}px"
							style:height="{scaleBundle.reference?.height ?? scaleBundle.replica.height}px"
							style:opacity={opacityPct / 100}
						/>
					{/if}
				</div>
			{:else if currentTrial !== null && scaleBundle.reference !== null}
				{@const s = scaleBundle.pxPerMm}
				{@const r = currentTrial.region}
				{@const cw = r.w * s}
				{@const ch = r.h * s}
				<div class="blind-head">
					<span title="Current blind trial index"
						>Trial {blindIndex + 1} / {blindTrialsState.length}</span
					>
				</div>
				<div class="blind-pair">
					<div class="ab-slot" title="Blind slot A">
						<div class="ab-label" title="First crop">A</div>
						<div class="crop-outer" style:width="{cw}px" style:height="{ch}px">
							<div
								class="crop-inner"
								style:width="{scaleBundle.replica.width}px"
								style:height="{scaleBundle.replica.height}px"
								style:transform="translate({-r.x * s}px, {-r.y * s}px)"
							>
								{#if slotSource(0) === 'replica'}
									<DeviceReplica
										{layout}
										map={practiceMapFor(layout)}
										disablePointerEvents={true}
									/>
								{:else}
									<img
										class="ref-img"
										src={effectiveRefUrl ?? ''}
										alt=""
										onload={onImgLoad}
										onerror={onImgError}
										style:width="{scaleBundle.reference.width}px"
										style:height="{scaleBundle.reference.height}px"
									/>
								{/if}
							</div>
						</div>
					</div>
					<div class="ab-slot" title="Blind slot B">
						<div class="ab-label" title="Second crop">B</div>
						<div class="crop-outer" style:width="{cw}px" style:height="{ch}px">
							<div
								class="crop-inner"
								style:width="{scaleBundle.replica.width}px"
								style:height="{scaleBundle.replica.height}px"
								style:transform="translate({-r.x * s}px, {-r.y * s}px)"
							>
								{#if slotSource(1) === 'replica'}
									<DeviceReplica
										{layout}
										map={practiceMapFor(layout)}
										disablePointerEvents={true}
									/>
								{:else}
									<img
										class="ref-img"
										src={effectiveRefUrl ?? ''}
										alt=""
										onload={onImgLoad}
										onerror={onImgError}
										style:width="{scaleBundle.reference.width}px"
										style:height="{scaleBundle.reference.height}px"
									/>
								{/if}
							</div>
						</div>
					</div>
				</div>
				<fieldset class="blind-form">
					<legend class="sr-only">Blind trial response</legend>
					<div class="choices" title="Which crop matched the physical deck better">
						<label
							><input type="radio" name="ab" bind:group={choiceLetter} value="A" /> A looked more like
							the deck</label
						>
						<label
							><input type="radio" name="ab" bind:group={choiceLetter} value="B" /> B looked more like
							the deck</label
						>
					</div>
					<div class="rating" title="Subjective match quality from 1 (poor) to 5 (excellent)">
						<span>Rating</span>
						{#each [1, 2, 3, 4, 5] as n (n)}
							<label
								><input type="radio" name="rating" bind:group={ratingVal} value={String(n)} />
								{n}</label
							>
						{/each}
					</div>
					<button
						type="button"
						title="Save this trial and go to the next"
						disabled={choiceLetter === null || ratingVal === null}
						onclick={nextBlindTrial}>Next</button
					>
					<button
						type="button"
						title="Download JSON report of answered trials"
						disabled={answeredCount === 0}
						onclick={downloadJson}>Download JSON</button
					>
				</fieldset>
			{:else}
				<p class="hint" title="Blind mode needs a loaded reference image">
					Load a reference image (URL or file) to use blind mode.
				</p>
			{/if}
		{/if}
	{/if}
</main>

<style>
	.compare {
		padding: 16px;
		max-width: 1600px;
		margin: 0 auto;
		background: #121418;
		color: #e8ebf0;
		min-height: 100vh;
		box-sizing: border-box;
	}
	h1 {
		font-size: 16px;
		margin: 0 0 4px;
	}
	.note {
		font-size: 12px;
		margin: 0 0 12px;
	}
	.toolbar {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 12px;
		margin-bottom: 12px;
		font-size: 12px;
	}
	.modes button {
		margin-right: 4px;
		padding: 4px 8px;
		cursor: pointer;
	}
	.modes button.active {
		background: #2b4a7a;
		color: #e8ebf0;
	}
	.file-row {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.opacity {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.readout {
		font-variant-numeric: tabular-nums;
	}
	.error {
		color: #ff8a8a;
	}
	.hint {
		color: #c9d0dc;
		font-size: 12px;
	}
	.split {
		display: flex;
		flex-direction: row;
		gap: 16px;
		overflow-x: auto;
		align-items: flex-start;
	}
	.lane {
		flex: 0 0 auto;
	}
	.lane-label {
		font-size: 12px;
		margin: 0 0 6px;
		font-weight: 600;
	}
	.replica-wrap {
		width: 100%;
	}
	.placeholder {
		font-size: 12px;
		color: #a9b0bc;
	}
	.ref-img {
		display: block;
	}
	.overlay-stack {
		position: relative;
		margin-top: 8px;
	}
	.overlay-bottom {
		position: absolute;
		top: 0;
		left: 0;
		width: 100%;
	}
	.ref-overlay {
		position: absolute;
		top: 0;
		left: 0;
		pointer-events: none;
	}
	.blind-head {
		font-size: 12px;
		margin-bottom: 8px;
	}
	.blind-pair {
		display: flex;
		gap: 16px;
		flex-wrap: wrap;
	}
	.ab-slot {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.ab-label {
		font-size: 14px;
		font-weight: 700;
	}
	.crop-outer {
		overflow: hidden;
		border: 1px solid #7a8291;
		border-radius: 4px;
	}
	.crop-inner {
		position: relative;
		transform-origin: 0 0;
	}
	.blind-form {
		margin-top: 16px;
		border: 1px solid #3a4048;
		padding: 12px;
		font-size: 12px;
	}
	.choices label,
	.rating label {
		margin-right: 12px;
	}
	.rating {
		margin: 8px 0;
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		border: 0;
	}
</style>
