<script lang="ts">
	/**
	 * Transition try-er panel (TRYER-14): audition N rendered candidate
	 * transitions for one pair and pick one. Every control has a CLI, HTTP and
	 * MCP twin (opendj dj try / POST /api/v1/dj/try / try_transitions).
	 *
	 * Audio is each candidate's render WAV from the server. All candidates
	 * start at the same outgoing bar, so switching keeps the playback position:
	 * a seamless A/B rather than a restart.
	 */
	import { onDestroy } from 'svelte';
	import { API_BASE } from '$lib/api/base';
	import { pickCandidate, startTry, undoPick } from '$lib/api/dj-try';
	import {
		groupDividerIndex,
		CHIP_LETTERS,
		chipOrder,
		isVetoed,
		keyAction,
		NOT_IMPLEMENTED_TITLE,
		redFlags,
		scoreBarPercent,
		shuffledOrder,
		unmeasured,
		useBestTarget,
		shortcutsYieldTo,
		createAuditioner,
		type TryCandidate,
		type TryJob
	} from './tryer-model';

	let { outId, inId, n, contextBars }: { outId: string; inId: string; n: number; contextBars: number } = $props();

	let job = $state<TryJob | null>(null);
	let error = $state<string | null>(null);
	let busy = $state(false);
	let blindOrder = $state<string[] | null>(null);
	let currentId = $state<string | null>(null);
	let lastId = $state<string | null>(null);
	let playing = $state(false);
	const auditioner =
		typeof Audio === 'undefined'
			? null
			: createAuditioner((url) => new Audio(url), {
					onEnded: () => (playing = false),
					// A render that is gone (404 render_missing) or undecodable must not look like it is playing.
					onFailure: (candidateId, reason) => {
						playing = false;
						error = `could not play candidate ${candidateId}: ${reason}`;
					}
				});

	const chips = $derived(job === null ? [] : chipOrder(job, blindOrder));
	const bestId = $derived(job === null ? null : useBestTarget(job));
	const divider = $derived(blindOrder === null ? groupDividerIndex(chips) : null);

	async function run(action: () => Promise<TryJob>): Promise<void> {
		busy = true;
		error = null;
		try {
			job = await action();
		} catch (failure) {
			error = failure instanceof Error ? failure.message : String(failure);
		} finally {
			busy = false;
		}
	}

	function render(): void {
		auditioner?.reset();
		playing = false;
		job = null; // the previous job is never actionable while (or after) a new render runs
		blindOrder = null;
		currentId = lastId = null;
		void run(() => startTry(outId, inId, n, contextBars));
	}

	function play(candidate: TryCandidate): void {
		if (auditioner === null) throw new Error('no HTMLAudioElement in this environment');
		if (currentId !== candidate.id) {
			lastId = currentId;
			currentId = candidate.id;
		}
		playing = true;
		auditioner.play(candidate.id, `${API_BASE}${candidate.render_url}`); // a server path; resolve it like every API call
	}

	function stop(): void {
		auditioner?.stop();
		playing = false;
	}

	function toggle(): void {
		const current = chips.find((chip) => chip.id === currentId) ?? chips[0];
		if (current === undefined) return;
		if (playing) stop();
		else play(current);
	}

	function swap(): void {
		const last = chips.find((chip) => chip.id === lastId);
		if (last !== undefined) play(last);
	}

	function pick(candidateId: string | null): void {
		if (job === null || candidateId === null) return;
		const current = job;
		void run(() => pickCandidate(current, candidateId));
	}

	function undo(): void {
		if (job === null) return;
		const current = job;
		void run(() => undoPick(current));
	}

	function toggleBlind(): void {
		if (job === null) return;
		blindOrder = blindOrder === null ? shuffledOrder(job.candidates.map((c) => c.id), Math.random) : null;
	}

	function cancel(): void {
		stop();
		currentId = lastId = null;
	}

	function onKey(event: KeyboardEvent): void {
		if (event.target instanceof HTMLElement && shortcutsYieldTo(event.target)) return;
		const action = keyAction(event.key);
		if (action === null || job === null) return;
		event.preventDefault();
		switch (action.kind) {
			case 'play': {
				const chip = chips[action.slot];
				if (chip !== undefined) play(chip);
				break;
			}
			case 'toggle':
				toggle();
				break;
			case 'swap':
				swap();
				break;
			case 'pick':
				pick(currentId);
				break;
			case 'blind':
				toggleBlind();
				break;
			case 'cancel':
				cancel();
				break;
			default: {
				const exhaustive: never = action;
				throw new Error(`Unhandled try-er action: ${JSON.stringify(exhaustive)}`);
			}
		}
	}

	onDestroy(stop);
</script>

<svelte:window onkeydown={onKey} />

<section class="tryer" aria-label="Transition try-er">
	<header>
		<h2>Try transitions</h2>
		<span class="pair" title="Outgoing and incoming track stable ids">{outId} → {inId}</span>
		<button onclick={render} disabled={busy}>{job === null ? 'Render candidates' : 'Render again'}</button>
		<button disabled title={NOT_IMPLEMENTED_TITLE}>Render more (R)</button>
		<button disabled title={NOT_IMPLEMENTED_TITLE}>Rank up/down (J/K)</button>
		{#if job !== null}
			<button onclick={toggleBlind} aria-pressed={blindOrder !== null} title="Hide scores and shuffle the chips (B)">
				Blind
			</button>
			<button
				onclick={() => pick(bestId)}
				disabled={busy || bestId === null}
				title={bestId === null
					? job.best_reason
					: 'Pick the highest-scoring candidate with measured alignment and no veto'}
			>
				Use best
			</button>
		{/if}
	</header>

	{#if busy}<p role="status">Working...</p>{/if}
	{#if error !== null}<p class="error" role="alert">{error}</p>{/if}

	{#if job !== null}
		<p class="meta">
			<span title="Scorer version that ranked these candidates">scorer {job.scorer_version}</span>
			<span title="Bars of context scored on each side of the transition">{job.context_bars} context bars</span>
			{#if job.pick !== null}
				<span class="picked">
					picked {job.pick.picked_candidate_id}
					<button onclick={undo} disabled={busy}>Undo pick</button>
				</span>
			{/if}
		</p>
		<ol class="chips">
			{#each chips as chip, slot (chip.id)}
				{@const bar = scoreBarPercent(chip)}
				{#if slot === 0 && blindOrder === null}
					<li class="group" title="Candidates rank within groups: measured alignment first">{chip.rank_group}</li>
				{:else if slot === divider}
					<li
						class="group divider"
						title="Alignment could not be measured for these, so they rank below every measured candidate and are never best"
					>
						{chip.rank_group}
					</li>
				{/if}
				<li
					class:current={chip.id === currentId}
					class:vetoed={isVetoed(chip)}
					class:picked={job.pick?.picked_candidate_id === chip.id}
				>
					<button class="chip" onclick={() => play(chip)} title={slot < 6 ? `Play this candidate's render (key ${slot + 1})` : "Play this candidate's render"}>
						<strong>{CHIP_LETTERS[slot]}</strong>
						{#if blindOrder === null}
							<span class="style">{chip.style}, <span title="Transition length in bars">{chip.bars}</span> bars</span>
						{/if}
					</button>
					{#if blindOrder === null}
						<div class="score">
							{#if bar === null}
								<span title="Fewer than 4 of 7 core factors were measured, so no score is given">unscored</span>
							{:else}
								<span class="bar" style:width="{bar}%"></span>
								<span title="Composite transition score M, 0 to 1 (0 when a veto fired)">{chip.score?.toFixed(2)}</span>
							{/if}
							<span title="Core quality factors the scorer measured, out of all core factors">
								{chip.measured.k} of {chip.measured.n} measured
							</span>
						</div>
						{#if isVetoed(chip)}<span class="veto" title="A hard check failed; this candidate scores 0">VETO</span>{/if}
						<ul class="flags">
							{#each redFlags(chip) as flag (flag)}<li title="Veto: measured value and its limit">{flag}</li>{/each}
							{#each unmeasured(chip) as gap (gap)}<li class="gap" title="Not measured, so not counted">{gap}</li>{/each}
						</ul>
						<span class="timing" title="Milliseconds to render, then to score, this candidate">
							{chip.render_ms === null ? `not rendered: ${chip.refused}` : `${chip.render_ms.toFixed(0)} ms render`}, {chip.score_ms.toFixed(
								0
							)} ms score
						</span>
					{/if}
					<button
						onclick={() => pick(chip.id)}
						disabled={busy || job.pick !== null}
						title={job.pick === null ? 'Pick this candidate (Enter)' : 'Undo the current pick first'}
					>
						Pick
					</button>
				</li>
			{/each}
		</ol>
		<p class="keys">1-6 play, Space play/stop, X swap to last, Enter pick, B blind, Esc cancel</p>
		<p class="deferred" title={NOT_IMPLEMENTED_TITLE}>Overlap waveforms and lane curves: not implemented</p>
	{/if}
</section>

<style>
	.tryer {
		display: grid;
		gap: 0.75rem;
		padding: 1rem;
	}
	header {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		align-items: center;
	}
	.chips {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
		gap: 0.5rem;
		list-style: none;
		padding: 0;
	}
	.chips > li {
		border: 1px solid var(--border, #555);
		border-radius: 6px;
		padding: 0.5rem;
		display: grid;
		gap: 0.25rem;
	}
	.chips > li.group {
		grid-column: 1 / -1;
		border: none;
		padding: 0;
		font-weight: 600;
	}
	.chips > li.divider {
		border-top: 2px dashed var(--border, #555);
		padding-top: 0.5rem;
	}
	.chips > li.current {
		outline: 2px solid var(--accent, #4af);
	}
	.chips > li.vetoed {
		opacity: 0.7;
	}
	.chips > li.picked {
		border-color: var(--ok, #4c4);
	}
	.score {
		position: relative;
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}
	.bar {
		display: inline-block;
		height: 0.4rem;
		background: var(--accent, #4af);
	}
	.veto {
		color: var(--danger, #e44);
		font-weight: 700;
	}
	.flags {
		margin: 0;
		padding-left: 1rem;
		font-size: 0.85em;
	}
	.flags .gap {
		opacity: 0.7;
	}
	.error {
		color: var(--danger, #e44);
	}
	.timing,
	.meta,
	.keys,
	.deferred {
		font-size: 0.8em;
		opacity: 0.8;
	}
</style>
