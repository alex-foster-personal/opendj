import { useEffect, useRef, type CSSProperties, type MouseEvent, type ReactElement } from 'react';

import type { DeckState } from '$lib/rb/deck-state-types';
import {
	createChipClickArbiter,
	stemChipDbLabel,
	stemChipLevelView,
	stemChipWheelParams,
	type ChipClickArbiter,
	type StemChipWheelParams
} from '$lib/rb/stem-chip-level';
import { stemCssColor } from '$lib/rb/stem-colors';
import type { SeparateActionView } from '$lib/rb/stem-separate-view';
import { quickModeActive, type StemQuickMode } from '$lib/rb/stem-quick-mode';
import { stemStatusView } from '$lib/rb/stem-status';
import type { StemControl } from '$lib/rb/stem-types';
import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';

import './StemRow.react.css';

// React twin of StemRow.svelte: same DOM, classes, ARIA and behavior, held to
// the same contract (StemRow.contract.ts). Click toggles mute; Shift+click
// toggles solo; scroll sets the level; double-click returns it to unity.

export type StemRowProps = {
	/** Only what the twin reads, so a caller can pass a plain snapshot. */
	deck: Pick<DeckState, 'deck_id' | 'stems'>;
	pending: boolean;
	/** Namespace for chip testids: `deck` keeps `stem-*-deck-N`; mixer uses `channel`. */
	testIdScope?: 'deck' | 'channel';
	onMute: (stem: StemControl) => Promise<void>;
	onSolo: (stem: StemControl) => Promise<void>;
	/** STEMPERF-11699: wheel over a live chip sets that stem's level; double-click returns it to unity. */
	onGain: (stem: StemControl, value: number) => void;
	/** STEM-48: retry a failed stem load, or start one that is held. */
	onLoad?: () => Promise<void>;
	/** STEM-77: the "Separate this track" action on a NO STEMS deck, or null. */
	separate?: SeparateActionView | null;
	onSeparate?: () => Promise<void>;
	/** STEMPERF-11695: apply an ACA / INST / ALL quick mode. Deck only. */
	onQuickMode?: (mode: StemQuickMode) => Promise<void>;
};

const QUICK: readonly { mode: StemQuickMode; label: string; tip: string }[] = [
	{ mode: 'aca', label: 'ACA', tip: 'Acapella: only VOCAL plays' },
	{ mode: 'inst', label: 'INST', tip: 'Instrumental: VOCAL muted, every solo cleared' },
	{ mode: 'all', label: 'ALL', tip: 'Everything: every mute and solo cleared, gains kept' }
];

const STEMS: readonly { id: StemControl; label: string }[] = [
	{ id: 'vocal', label: 'VOCAL' },
	{ id: 'instrumental', label: 'INST' },
	{ id: 'drums', label: 'DRUMS' },
	{ id: 'bass', label: 'BASS' },
	{ id: 'other', label: 'HARM' }
];

/** Attaches the shared wheel action; React's onWheel is passive and cannot preventDefault. */
function useWheelAdjust(params: StemChipWheelParams) {
	const ref = useRef<HTMLButtonElement>(null);
	const handle = useRef<ReturnType<typeof wheelAdjust> | null>(null);
	useEffect(() => {
		const el = ref.current;
		if (el === null) return;
		handle.current = wheelAdjust(el, params);
		return () => {
			handle.current?.destroy();
			handle.current = null;
		};
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, []);
	useEffect(() => {
		handle.current?.update(params);
	});
	return ref;
}

function ChipWheel({ params, render }: { params: StemChipWheelParams; render: (ref: ReturnType<typeof useWheelAdjust>) => ReactElement }) {
	return render(useWheelAdjust(params));
}

const cls = (...parts: (string | false)[]): string => parts.filter(Boolean).join(' ');

export default function StemRow({
	deck,
	pending,
	testIdScope = 'deck',
	onMute,
	onSolo,
	onGain,
	onLoad,
	separate = null,
	onSeparate,
	onQuickMode
}: StemRowProps) {
	const stems = deck.stems;
	const visibleStems = STEMS.filter(
		(stem) => (stem.id !== 'bass' && stem.id !== 'other') || stems.available_controls.includes(stem.id)
	);
	const ready = stems.status === 'ready';
	const unavailable = (stem: StemControl): boolean => ready && !stems.available_controls.includes(stem);
	const view = stemStatusView(stems);
	const statusTip = view.tip;
	const quickActive = quickModeActive(stems);

	function chipTip(stem: StemControl, label: string): string {
		if (unavailable(stem)) {
			return (
				`${label} is not a separate stem in this ${stems.layout ?? 'bundle'} ` +
				`- it is mixed into INST, so it cannot be muted on its own`
			);
		}
		const ctrl = stems.controls[stem];
		const state = ctrl.solo ? 'solo selected' : ctrl.muted ? 'muted now' : 'unmuted (other solo/group controls still apply)';
		const level = stemChipDbLabel(ctrl.gain);
		return `${label} stem is ${state}, level ${level}. Click to mute, Shift+click to solo, scroll for level, double-click for unity. ${statusTip}`;
	}

	// Latest callbacks, read at fire time so the arbiter is created once.
	const latest = useRef({ onMute, onSolo, onGain });
	latest.current = { onMute, onSolo, onGain };
	const arbiterRef = useRef<ChipClickArbiter | null>(null);
	if (arbiterRef.current === null) {
		arbiterRef.current = createChipClickArbiter({
			onMute: (stem) => latest.current.onMute(stem),
			onSolo: (stem) => latest.current.onSolo(stem),
			onGain: (stem, value) => latest.current.onGain(stem, value)
		});
	}
	const arbiter = arbiterRef.current;

	function toggle(event: MouseEvent, stem: StemControl): void {
		arbiter.click({ stem, shift: event.shiftKey });
	}

	return (
		<div
			className="stems"
			role="group"
			aria-label={`stem controls deck ${deck.deck_id}`}
			data-stems-status={stems.status}
			data-stems-state={view.name}
			data-stems-phase={stems.load?.phase ?? undefined}
			title={statusTip}
		>
			<span className="mute">MUTE</span>
			{visibleStems.map((stem) => {
				const ctrl = stems.controls[stem.id];
				const off = unavailable(stem.id);
				const level = stemChipLevelView(ctrl.gain);
				const params = stemChipWheelParams({
					stem: stem.id,
					gain: () => stems.controls[stem.id].gain,
					live: ready && !off,
					step: WHEEL_STEP.knob,
					onGain: (s, v) => latest.current.onGain(s, v)
				});
				return (
					<ChipWheel key={stem.id} params={params} render={(ref) => (
					<button
						ref={ref}
						className={cls('chip', ctrl.muted && 'muted', ctrl.solo && 'solo', off && 'unavailable')}
						disabled={!ready || off}
						aria-busy={pending}
						title={chipTip(stem.id, stem.label)}
						aria-label={`${stem.label} stem mute deck ${deck.deck_id}; Shift+click solo`}
						data-testid={`stem-${stem.id}-${testIdScope}-${deck.deck_id}`}
						data-unavailable={off}
						aria-pressed={ctrl.muted}
						data-performance-control={`stem-${stem.id}`}
						data-muted={ctrl.muted}
						data-solo={ctrl.solo}
						data-stem-gain={ctrl.gain}
						style={{ '--chip-color': stemCssColor(stem.id) } as CSSProperties}
						onClick={(event) => toggle(event, stem.id)}
						onDoubleClick={() => arbiter.doubleClick(stem.id)}
					>
						{testIdScope === 'channel' ? stem.label.slice(0, 3) : stem.label}
						{!level.unity && <span className="level" aria-hidden="true" style={{ height: `${level.barPx}px` }} />}
					</button>
					)} />
				);
			})}
			{onQuickMode !== undefined &&
				QUICK.map((q) => (
					<button
						key={q.mode}
						type="button"
						className={cls('quick', quickActive === q.mode && 'active')}
						disabled={!ready}
						aria-busy={pending}
						aria-pressed={quickActive === q.mode}
						data-active={quickActive === q.mode}
						data-testid={`stem-quick-${q.mode}-${testIdScope}-${deck.deck_id}`}
						data-performance-control={`stem-quick-${q.mode}`}
						title={`${q.tip}. ${statusTip}`}
						aria-label={`${q.label} quick stem mode deck ${deck.deck_id}`}
						onClick={async () => await onQuickMode(q.mode)}
					>
						{q.label}
					</button>
				))}
			{onLoad !== undefined && view.label !== '' && (view.action !== null ? (
					<button
						className={cls('status', 'action', view.name === 'error' && 'failed')}
						title={view.tip}
						aria-label={`${view.tip} Deck ${deck.deck_id}.`}
						data-testid={`stem-status-${testIdScope}-${deck.deck_id}`}
						data-stems-state={view.name}
						data-stems-action={view.action}
						data-performance-control="stem-load"
						onClick={async () => await onLoad()}
					>
						{view.label}
					</button>
				) : (
					<span
						className={cls('status', view.busy && 'busy')}
						role="status"
						title={view.tip}
						data-testid={`stem-status-${testIdScope}-${deck.deck_id}`}
						data-stems-state={view.name}
					>
						{view.label}
					</span>
				))}
			{onLoad !== undefined && view.label !== '' && view.name === 'none' && separate !== null && onSeparate !== undefined && (
				<button
					className="status action separate"
					disabled={separate.disabled}
					title={separate.tip}
					aria-label={`${separate.tip} Deck ${deck.deck_id}.`}
					data-testid={`stem-separate-${testIdScope}-${deck.deck_id}`}
					data-performance-control="stem-separate"
					onClick={async () => await onSeparate()}
				>
					{separate.label}
				</button>
			)}
		</div>
	);
}
