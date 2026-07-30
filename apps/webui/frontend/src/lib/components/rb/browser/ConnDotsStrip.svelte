<script lang="ts">
	/**
	 * Bottom-left connectivity strip: soft Spotify/Rekordbox/djay (light) above
	 * bright LIB/BE/FE, plus a privacy-gated bug-report affordance.
	 */
	import { deckStates as decks, DECK_IDS } from '$lib/rb/audio-engine.svelte';
	import { RB_API_BASE } from '$lib/rb/api-rb';
	import { requestLocalAgent } from '$lib/rb/local-agent';
	import { pushToast } from '$lib/stores.svelte';
	import {
		anyBrightDown,
		brightDotTip,
		buildClientBugReportMarkdown,
		defaultBugIssueTitle,
		githubBugIssueUrl,
		softDotTip,
		tipTitle,
		type SoftDotState
	} from '$lib/rb/conn-dot-tips';

	/**
	 * Native `title` on dots duplicates the fancy `.conn-panel` hover tip
	 * (double tooltip). Keep false while the panel is the tip UI; flip true
	 * later for a cheap no-panel mode (perf / reduced-motion) without ripping
	 * out tipTitle(). aria-label stays either way for a11y.
	 */
	const CONN_DOT_NATIVE_TITLE = false;

	type Props = {
		libUp: boolean;
		beUp: boolean;
		feUp: boolean;
		softDots: SoftDotState[];
	};

	let { libUp, beUp, feUp, softDots }: Props = $props();

	let hoverId = $state<string | null>(null);
	let panelOpen = $state(false);

	const decksBusy = $derived(
		DECK_IDS.some((d) => decks[d].playing === true || decks[d].audible === true)
	);
	const brightBad = $derived(anyBrightDown(libUp, beUp, feUp));

	const tipsById = $derived.by(() => {
		const m = new Map<string, ReturnType<typeof softDotTip>>();
		m.set('lib', brightDotTip('lib', libUp));
		m.set('be', brightDotTip('be', beUp));
		m.set('fe', brightDotTip('fe', feUp));
		for (const d of softDots) m.set(d.id, softDotTip(d));
		return m;
	});

	const activeTip = $derived(hoverId !== null ? (tipsById.get(hoverId) ?? null) : null);

	function onEnter(id: string): void {
		hoverId = id;
		panelOpen = true;
	}

	function onLeaveStrip(): void {
		hoverId = null;
		panelOpen = false;
	}

	function _reportInput() {
		return {
			libUp,
			beUp,
			feUp,
			soft: softDots,
			apiBase: RB_API_BASE,
			origin: typeof window !== 'undefined' ? window.location.origin : '',
			userAgent: typeof navigator !== 'undefined' ? navigator.userAgent : '',
			decksPlayingOrAudible: decksBusy
		};
	}

	function downloadClientReport(): void {
		if (decksBusy) {
			pushToast('Stop decks / finish the mix before exporting a bug report', 'error');
			return;
		}
		const md = buildClientBugReportMarkdown(_reportInput());
		const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		const ts = new Date().toISOString().replace(/[:.]/g, '-');
		a.href = url;
		a.download = `opendj-bug-report-client-${ts}.md`;
		a.click();
		URL.revokeObjectURL(url);
		pushToast('Downloaded local bug report markdown (nothing was sent)', 'info');
	}

	function openGithubDraft(): void {
		if (decksBusy) {
			pushToast('Stop decks / finish the mix before opening a GitHub bug draft', 'error');
			return;
		}
		const input = _reportInput();
		const body =
			buildClientBugReportMarkdown(input) +
			'\n\n_Paste output of `uv run python scripts/bug_report_bundle.py` below if available._\n';
		const url = githubBugIssueUrl(defaultBugIssueTitle(input), body);
		window.open(url, '_blank', 'noopener,noreferrer');
		pushToast('Opened GitHub issue draft in browser (review before submit)', 'info');
	}

	async function copyAskPrompts(): Promise<void> {
		const tip = activeTip;
		const lines =
			tip?.askAgent && tip.askAgent.length > 0
				? tip.askAgent
				: [
						'Ask agent: run `just watch-servers-once` and report LIB/BE/FE + PEERS.',
						'Ask agent: follow `.agents/skills/bug-report-diagnostics/SKILL.md`.'
					];
		try {
			await navigator.clipboard.writeText(lines.join('\n'));
			pushToast('Copied ask-agent prompts', 'info');
		} catch {
			pushToast('Clipboard blocked; select text from the hover panel', 'error');
		}
	}

	let agentBusy = $state(false);

	async function askLocalAgent(kind: 'claude' | 'servers'): Promise<void> {
		if (agentBusy) return;
		agentBusy = true;
		const action = kind === 'claude' ? 'claude-run-servers' : 'run-servers';
		try {
			const res = await requestLocalAgent(action);
			const label =
				kind === 'claude' ? 'Claude Opus (fix servers)' : 'servers-only start-if-down';
			pushToast(
				`Local agent accepted: ${label}` + (res.pid != null ? ` (pid ${res.pid})` : ''),
				'info'
			);
		} catch (err) {
			pushToast(err instanceof Error ? err.message : String(err), 'error');
		} finally {
			agentBusy = false;
		}
	}
</script>

<div
	class="conn-dots"
	class:attention={brightBad}
	aria-label="server connectivity"
	role="group"
	onmouseleave={onLeaveStrip}
>
	{#each softDots as dot (dot.id)}
		{@const tip = softDotTip(dot)}
		<span
			class="conn-dot soft"
			class:up={dot.up}
			data-server={dot.id}
			title={CONN_DOT_NATIVE_TITLE ? tipTitle(tip) : undefined}
			aria-label={`${tip.name} ${tip.status}`}
			role="img"
			onmouseenter={() => onEnter(dot.id)}
		></span>
	{/each}
	{#each (['lib', 'be', 'fe'] as const) as id (id)}
		{@const up = id === 'lib' ? libUp : id === 'be' ? beUp : feUp}
		{@const tip = brightDotTip(id, up)}
		<span
			class="conn-dot"
			class:up
			data-server={id}
			title={CONN_DOT_NATIVE_TITLE ? tipTitle(tip) : undefined}
			aria-label={`${tip.name} ${tip.status}`}
			role="img"
			onmouseenter={() => onEnter(id)}
		></span>
	{/each}

	<button
		type="button"
		class="conn-agent"
		class:hot={brightBad}
		class:busy={agentBusy}
		title="Agent fix servers: asks Claude Code (Opus) via local launchd on :18765 (works even if BE is down). Requires `just install-local-agent`. Click = Claude; right-click = servers-only (no LLM). See docs/opendj-local-agent.md"
		aria-label="Agent fix servers via local launchd"
		disabled={agentBusy}
		onclick={(e) => {
			e.stopPropagation();
			void askLocalAgent('claude');
		}}
		oncontextmenu={(e) => {
			e.preventDefault();
			e.stopPropagation();
			void askLocalAgent('servers');
		}}
	>
		A
	</button>

	<button
		type="button"
		class="conn-bug"
		class:hot={brightBad}
		class:blocked={decksBusy}
		title={decksBusy
			? 'Bug report disabled while any deck is playing or audible - finish the mix / stop decks first'
			: 'Download a local diagnostics markdown and/or open a GitHub bug draft (nothing auto-sent). Fuller bundle: uv run python scripts/bug_report_bundle.py'}
		aria-label={decksBusy ? 'bug report blocked while playing' : 'prepare bug report'}
		onclick={(e) => {
			e.stopPropagation();
			if (decksBusy) {
				pushToast('Finish the mix / stop decks before sharing diagnostics', 'error');
				return;
			}
			downloadClientReport();
		}}
		oncontextmenu={(e) => {
			e.preventDefault();
			openGithubDraft();
		}}
	>
		!
	</button>

	{#if panelOpen && activeTip}
		<div class="conn-panel" role="tooltip">
			<div class="conn-panel-h">
				<span class="conn-panel-name">{activeTip.name}</span>
				<span class="conn-panel-st" class:bad={!activeTip.up}>{activeTip.status}</span>
			</div>
			<p class="conn-panel-m">{activeTip.meaning}</p>
			{#if activeTip.askAgent && activeTip.askAgent.length > 0}
				<ul class="conn-panel-ask">
					{#each activeTip.askAgent as line}
						<li>{line}</li>
					{/each}
				</ul>
				<button type="button" class="conn-panel-copy" onclick={copyAskPrompts}>
					Copy ask-agent lines
				</button>
			{/if}
			<div class="conn-panel-actions">
				<button type="button" class="conn-panel-btn" disabled={decksBusy} onclick={downloadClientReport}>
					Download report
				</button>
				<button type="button" class="conn-panel-btn" disabled={decksBusy} onclick={openGithubDraft}>
					Open GitHub draft
				</button>
			</div>
			{#if decksBusy}
				<p class="conn-panel-warn">Sharing blocked: a deck is playing or audible. Stop decks first.</p>
			{/if}
			<p class="conn-panel-hint">
				Soft = light green (non-blocking). Bright = LIB/BE/FE. A = agent fix servers (launchd
				:18765). Right-click ! for GitHub draft.
			</p>
		</div>
	{/if}
</div>

<style>
	.conn-dots {
		position: absolute;
		left: 10px;
		bottom: 22px;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 3px;
		z-index: 6;
		pointer-events: auto;
	}
	.conn-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: #3a4048;
		box-shadow: inset 0 0 0 1px #23282f;
		cursor: help;
	}
	.conn-dot.up {
		background: var(--rb-green, #35c04f);
		box-shadow: 0 0 4px color-mix(in srgb, var(--rb-green, #35c04f) 70%, transparent);
	}
	.conn-dot.soft.up {
		background: color-mix(in srgb, var(--rb-green, #35c04f) 55%, #c8f5d0);
		box-shadow: 0 0 3px color-mix(in srgb, var(--rb-green, #35c04f) 40%, transparent);
	}
	.conn-dot.soft:not(.up) {
		background: #2e333a;
		opacity: 0.85;
	}
	.conn-agent,
	.conn-bug {
		margin-top: 2px;
		width: 14px;
		height: 14px;
		padding: 0;
		border: 1px solid #4a515a;
		border-radius: 3px;
		background: #1c2128;
		color: #9aa3ad;
		font: 700 10px/1 ui-sans-serif, system-ui, sans-serif;
		cursor: pointer;
		opacity: 0.55;
	}
	.conn-agent.hot,
	.conn-bug.hot {
		opacity: 1;
		color: #f0c14a;
		border-color: #8a6a20;
	}
	.conn-agent.busy {
		opacity: 0.4;
		cursor: wait;
	}
	.conn-bug.blocked {
		opacity: 0.35;
		cursor: not-allowed;
	}
	.conn-dots.attention .conn-agent.hot,
	.conn-dots.attention .conn-bug.hot {
		box-shadow: 0 0 6px color-mix(in srgb, #f0c14a 45%, transparent);
	}
	.conn-panel {
		position: absolute;
		left: 18px;
		bottom: 0;
		width: min(340px, 70vw);
		padding: 8px 10px;
		border: 1px solid var(--rb-border, #2e333a);
		border-radius: 4px;
		background: color-mix(in srgb, var(--rb-panel, #1a1e24) 94%, #000);
		color: var(--rb-text, #d7dde5);
		font-size: 11px;
		line-height: 1.35;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
		pointer-events: auto;
	}
	.conn-panel-h {
		display: flex;
		justify-content: space-between;
		gap: 8px;
		margin-bottom: 4px;
		font-weight: 700;
	}
	.conn-panel-st {
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--rb-green, #35c04f);
	}
	.conn-panel-st.bad {
		color: #e07070;
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
	.conn-panel-copy,
	.conn-panel-btn {
		border: 1px solid #3a4048;
		border-radius: 3px;
		background: #12161c;
		color: var(--rb-text, #d7dde5);
		font: inherit;
		font-size: 10px;
		padding: 3px 6px;
		cursor: pointer;
	}
	.conn-panel-copy {
		margin-bottom: 6px;
	}
	.conn-panel-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		margin-bottom: 4px;
	}
	.conn-panel-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}
	.conn-panel-warn {
		margin: 0 0 4px;
		color: #e07070;
		font-weight: 600;
	}
	.conn-panel-hint {
		margin: 0;
		color: var(--rb-text-dim, #9aa3ad);
		font-size: 10px;
	}
</style>
