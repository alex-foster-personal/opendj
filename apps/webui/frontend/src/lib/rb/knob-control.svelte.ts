/**
 * Computer-as-decks knob control: selection, global scroll, inverse link pairs
 * with bass-preserving stagger, and central drag/scroll sensitivity.
 *
 * Shift+click toggles a dial in or out of an ORDERED selection set (MIXUX-13).
 * Scroll, arrow keys or the global wheel on any selected dial move every
 * selected dial by the same delta (common mode). A drag on a selected control
 * with 2+ selected moves the whole group, led by the control under the
 * pointer (R2, Wed 7 Oct 2026); Opt held flips one side (R3). Channel faders
 * join the same selection as `deck:fader`. Esc or a
 * click on empty space clears the set. Plain click/drag does not select.
 * Alt/Option+click two knobs to link (turn one down => other up).
 *
 * the maintainer, Tue 6 Oct 2026: "we had a feature where we could select multiple
 * knobs with shift held. on any click&drag then or scroll etc (as per usual
 * knob adjustments) it then adjusts both / all knobs up and down together -
 * to get around the not having physical knobs limitations. [...] up and right
 * is both / all knobs up, up and left is one up, one down".
 */

import { detectWheelInputKind, scaledWheelStep } from './wheel-adjust';
import { gainedStraightDrag } from './drag-sensitivity.svelte';

// ----- central sensitivity (tweak here; not buried in components) -----

export const KNOB_CFG = {
	/** Vertical drag pixels for a full 0..1 sweep (higher = less sensitive). */
	dragVerticalPx: 120,
	/** Horizontal drag pixels for a full 0..1 sweep (much larger = fine adjust). */
	dragHorizontalPx: 520,
	/** Wheel deltaY scale -> knob units (hover or selected). Scaled per input
	 * kind by WHEEL_SENSITIVITY, so a trackpad burst is not 3 steps a flick. */
	scrollStep: 0.028,
	/** Arrow-key nudge. */
	keyStep: 0.02,
	/** Pointer travel (px, per axis) a press may wobble and still be a click.
	 * Mouse jitter and touch contact motion both emit pointermove with no intent. */
	clickSlopPx: 4,
	/**
	 * Extra boost applied to the *rising* side of a linked pair, as a
	 * fraction of |delta|. Keeps bass energy from dipping while the cut/boost
	 * crosses through the middle.
	 */
	linkStagger: 0.18
} as const;

/** True once a press has travelled past the click slop on either axis. */
export function pointerTravelIsDrag(dx: number, dy: number): boolean {
	return Math.abs(dx) > KNOB_CFG.clickSlopPx || Math.abs(dy) > KNOB_CFG.clickSlopPx;
}

// ----- types -----

export type KnobRole =
	| 'trim'
	| 'high'
	| 'mid'
	| 'low'
	| 'filter'
	| 'hp-mix'
	| 'hp-level'
	| 'stem-vocal'
	| 'stem-instrumental'
	| 'stem-drums';

export interface KnobRef {
	id: string;
	getValue: () => number;
	setValue: (value: number) => void;
}

export interface LinkedPair {
	a: string;
	b: string;
}

// ----- state -----

export const knobUi = $state<{
	/** Shift+click selection, in the order the dials were selected. */
	selectedIds: string[];
	linkPendingId: string | null;
	link: LinkedPair | null;
	hoveredId: string | null;
	/** R4: the refused control and why, shown briefly next to it. */
	groupHint: { id: string; text: string } | null;
}>({
	selectedIds: [],
	linkPendingId: null,
	link: null,
	hoveredId: null,
	groupHint: null
});

/**
 * Per-id STACK, not a single ref: EqOverlay's knobs (LIBUX-05 cmd+E) mount
 * the same deck:band id as the mixer's always-mounted ChannelStrip knob for
 * the same parameter while raised, then unmount on close. A single-ref map
 * loses the mixer's registration permanently on that unregister. Register
 * pushes, unregister pops its own entry, so closing the overlay always
 * uncovers whichever control was registered before it - LIFO, which matches
 * mount order since EqOverlay is strictly nested inside the mixer's lifetime.
 */
const _registry = new Map<string, KnobRef[]>();
let _wheelBound = false;
let _clearBound = false;

function _activeRef(id: string): KnobRef | undefined {
	return _registry.get(id)?.at(-1);
}

// ----- helpers -----

export function knobId(deckId: number | 'hp', role: KnobRole): string {
	return `${deckId}:${role}`;
}

/**
 * Channel LEVEL fader id in this same selection registry. Same `deck:part`
 * shape as a dial id, so knobGroupOf puts a fader on its deck's channel and
 * a fader plus that deck's EQs count as ONE channel for the group rules.
 */
export function faderControlId(deckId: number): string {
	return `${deckId}:fader`;
}

export function clamp01(v: number): number {
	return Math.min(1, Math.max(0, v));
}

/** Inverse link with stagger on the rising side (bass-preserving cross). */
export function applyLinkedDelta(
	primary: number,
	secondary: number,
	delta: number,
	stagger: number = KNOB_CFG.linkStagger
): { primary: number; secondary: number } {
	let p = clamp01(primary + delta);
	let s = clamp01(secondary - delta);
	const rise = Math.abs(delta) * stagger;
	if (delta > 0) p = clamp01(p + rise);
	else if (delta < 0) s = clamp01(s + rise);
	return { primary: p, secondary: s };
}

// ----- multi-select (MIXUX-13), pure -----

/** R4 (the maintainer, Wed 7 Oct 2026): "limit shift click drag selection groups to two
 * channels but any number of items on that channel". */
export const GROUP_MAX_CHANNELS = 2;
export const GROUP_LIMIT_HINT = 'Group is limited to two channels';

/** The distinct channels (decks, or `hp`) of a selection, in first-selected order. */
export function selectionChannels(selection: readonly string[]): string[] {
	const out: string[] = [];
	for (const id of selection) {
		const group = knobGroupOf(id);
		if (!out.includes(group)) out.push(group);
	}
	return out;
}

/** True when adding `id` would put a third channel in the group. Removing never is. */
export function wouldExceedChannelLimit(selection: readonly string[], id: string): boolean {
	if (selection.includes(id)) return false;
	if (selection.some((s) => isTestControl(s) !== isTestControl(id))) return false;
	return selectionChannels([...selection, id]).length > GROUP_MAX_CHANNELS;
}

/** Settings' drag tester scopes its controls `test-a` / `test-b`: wired to nothing. */
const TEST_GROUP_PREFIX = 'test-';

/** True for a drag-tester control, which must never share a group or link with a live one. */
export function isTestControl(id: string): boolean {
	return knobGroupOf(id).startsWith(TEST_GROUP_PREFIX);
}

/**
 * Shift+click: toggle `id` in an ordered selection, keeping selection order.
 * A tester control and a live control never share a selection: clicking one
 * kind while the other is selected starts a fresh selection, so a tester drag
 * can never move a live control.
 */
export function toggleKnobSelection(selection: readonly string[], id: string): string[] {
	if (selection.some((s) => isTestControl(s) !== isTestControl(id))) return [id];
	return selection.includes(id) ? selection.filter((s) => s !== id) : [...selection, id];
}

/** The deck (or `hp`) a dial belongs to: the scope before the role in its id. */
export function knobGroupOf(id: string): string {
	const sep = id.indexOf(':');
	return sep === -1 ? id : id.slice(0, sep);
}

/** Common mode: every dial moves by the same delta, each clamped to 0..1. */
export function commonModeTargets(
	baselines: Readonly<Record<string, number>>,
	delta: number
): Record<string, number> {
	const out: Record<string, number> = {};
	for (const [id, value] of Object.entries(baselines)) out[id] = clamp01(value + delta);
	return out;
}

/** Single-dial drag: vertical is coarse, horizontal is fine adjust. */
export function singleDragDelta(dx: number, dy: number): number {
	return dy / KNOB_CFG.dragVerticalPx + dx / KNOB_CFG.dragHorizontalPx;
}

/**
 * R7 two-axis pad axes. X = channel A (the first-selected item's channel),
 * Y = channel B. A one-channel group puts the first item on X and the rest
 * on Y. So the +45 degree diagonal moves both the same way and the -45 degree
 * diagonal is the Opt-flip feel.
 */
export function padAxes(selection: readonly string[]): { x: string[]; y: string[] } {
	const channels = selectionChannels(selection);
	if (channels.length >= 2) {
		return {
			x: selection.filter((id) => knobGroupOf(id) === channels[0]),
			y: selection.filter((id) => knobGroupOf(id) !== channels[0])
		};
	}
	return { x: selection.slice(0, 1), y: selection.slice(1) };
}

/**
 * Pad targets from baselines: `dx` (right-positive) moves the X items and
 * `dy` (up-positive) the Y items, both in 0..1 units, each clamped. Null for
 * fewer than two items.
 */
export function padDragTargets(
	selection: readonly string[],
	baselines: Readonly<Record<string, number>>,
	dx: number,
	dy: number
): Record<string, number> | null {
	if (selection.length < 2) return null;
	const axes = padAxes(selection);
	const out: Record<string, number> = {};
	for (const id of selection) {
		const base = baselines[id];
		if (base === undefined) continue;
		out[id] = clamp01(base + (axes.x.includes(id) ? dx : dy));
	}
	return out;
}

/**
 * Keep the Alt inverse link inside a group move (Sol P1 on #5701). When a
 * linked dial is selected and its partner is NOT, the dial's move drives the
 * partner inversely with the usual stagger, exactly as a single-dial move
 * would.
 *
 * When BOTH dials of the pair are selected, the selection wins and the link is
 * not applied between them (CORE decision, orders board #5638, Tue 6 Oct 2026
 * 18:53Z). the maintainer's multi-select spec is "it then adjusts both / all knobs up
 * and down together", and the two-axis drag is his explicit way to split them
 * ("up and left is one up, one down"); a link overriding that inside the
 * selection would contradict both. `baselines` must carry the partner's value;
 * without it the targets are returned unchanged.
 */
export function applyLinkToTargets(
	selection: readonly string[],
	baselines: Readonly<Record<string, number>>,
	targets: Readonly<Record<string, number>>,
	link: LinkedPair | null
): Record<string, number> {
	const out = { ...targets };
	if (link === null) return out;
	const driver = selection.find((id) => id === link.a || id === link.b);
	if (driver === undefined) return out;
	const partner = driver === link.a ? link.b : link.a;
	// Both selected: the selection wins, each keeps its own selection move.
	if (selection.includes(partner)) return out;
	const driverBase = baselines[driver];
	const partnerBase = baselines[partner];
	const driverTarget = out[driver];
	if (driverBase === undefined || partnerBase === undefined || driverTarget === undefined) return out;
	const next = applyLinkedDelta(driverBase, partnerBase, driverTarget - driverBase);
	out[driver] = next.primary;
	out[partner] = next.secondary;
	return out;
}

/** Selectors for things a click on is NOT a click on empty space. */
const _NOT_EMPTY_SELECTOR =
	'[data-knob-id], button, a, input, select, textarea, label, [role="slider"], [role="button"], [role="menu"], [role="menuitem"], [contenteditable="true"]';

/** True when a pointerdown target is empty space, so it clears the selection. */
export function isEmptySpaceTarget(target: unknown): boolean {
	if (target === null || typeof target !== 'object') return false;
	const closest = (target as { closest?: (sel: string) => unknown }).closest;
	if (typeof closest !== 'function') return false;
	return closest.call(target, _NOT_EMPTY_SELECTOR) === null;
}

function _partnerOf(id: string): string | null {
	const link = knobUi.link;
	if (link === null) return null;
	if (link.a === id) return link.b;
	if (link.b === id) return link.a;
	return null;
}

// ----- registry -----

export function registerKnob(ref: KnobRef): void {
	const stack = _registry.get(ref.id);
	if (stack) stack.push(ref);
	else _registry.set(ref.id, [ref]);
	_ensureGlobalWheel();
	_ensureSelectionClear();
}

export function unregisterKnob(id: string): void {
	const stack = _registry.get(id);
	if (!stack) return;
	stack.pop();
	if (stack.length > 0) return;
	_registry.delete(id);
	// An earlier registrant for this id (e.g. the mixer knob a shadowing
	// EqOverlay widget sits on top of) surviving underneath means the user's
	// selection/link is still pointing at a live widget - only clear the
	// UI-state once the LAST registrant for this id is gone.
	//
	// Deferred one microtask (MIXUX-13): Knob.svelte's register effect re-runs
	// as the dial's value changes, which is a synchronous unregister+register
	// of the SAME id. Clearing here at once wiped the selection (and an Alt
	// link) on the first frame of every drag, measured live in Chromium. A
	// dial that is really gone is still pruned, just after the re-register
	// had its chance.
	queueMicrotask(() => {
		if (_registry.has(id)) return;
		if (knobUi.selectedIds.includes(id)) {
			knobUi.selectedIds = knobUi.selectedIds.filter((s) => s !== id);
		}
		if (knobUi.linkPendingId === id) knobUi.linkPendingId = null;
		if (knobUi.hoveredId === id) knobUi.hoveredId = null;
		if (knobUi.link !== null && (knobUi.link.a === id || knobUi.link.b === id)) {
			knobUi.link = null;
		}
	});
}

/** Test-only: clears the module-level registry stack between test cases. */
export function _resetKnobRegistryForTests(): void {
	_registry.clear();
}

export function isKnobSelected(id: string): boolean {
	return knobUi.selectedIds.includes(id);
}

/** The ordered selection, first-selected first. */
export function selectedKnobIds(): readonly string[] {
	return knobUi.selectedIds;
}

export function isKnobLinked(id: string): boolean {
	return _partnerOf(id) !== null || knobUi.linkPendingId === id;
}

export function isKnobHighlighted(id: string): boolean {
	return isKnobSelected(id) || isKnobLinked(id);
}

export function setKnobHovered(id: string | null): void {
	knobUi.hoveredId = id;
}

/** Replace the selection (agent bridge); duplicates collapse, order kept. */
export function setKnobSelection(ids: readonly string[]): void {
	const next: string[] = [];
	for (const id of ids) if (!next.includes(id)) next.push(id);
	if (next.some((id) => isTestControl(id) !== isTestControl(next[0]))) {
		throw new RangeError(`a selection cannot mix drag-tester and live controls; got ${next.join(', ')}`);
	}
	const channels = selectionChannels(next);
	if (channels.length > GROUP_MAX_CHANNELS) {
		throw new RangeError(`${GROUP_LIMIT_HINT}; got ${channels.join(', ')}`);
	}
	knobUi.selectedIds = next;
}

let _hintTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * Shift+click: toggle a control in or out of the ordered selection set. A
 * control on a third channel is refused, not swapped for the oldest (R4
 * assumption, flagged to the maintainer), and a short hint names the limit.
 */
export function shiftClickKnob(id: string): boolean {
	if (wouldExceedChannelLimit(knobUi.selectedIds, id)) {
		knobUi.groupHint = { id, text: GROUP_LIMIT_HINT };
		if (_hintTimer !== null) clearTimeout(_hintTimer);
		_hintTimer = setTimeout(() => {
			knobUi.groupHint = null;
			_hintTimer = null;
		}, 2500);
		return false;
	}
	knobUi.groupHint = null;
	knobUi.selectedIds = toggleKnobSelection(knobUi.selectedIds, id);
	return true;
}

/** Esc or a click on empty space. */
export function clearKnobSelection(): void {
	if (knobUi.selectedIds.length > 0) knobUi.selectedIds = [];
}

/** Alt/Option+click: arm link on first dial, complete on second, clear if same. */
export function altClickKnob(id: string): void {
	if (knobUi.linkPendingId === null) {
		knobUi.linkPendingId = id;
		return;
	}
	if (knobUi.linkPendingId === id) {
		knobUi.linkPendingId = null;
		knobUi.link = null;
		return;
	}
	if (isTestControl(knobUi.linkPendingId) !== isTestControl(id)) {
		// A tester dial never links to a live one: this click arms a fresh link.
		knobUi.linkPendingId = id;
		return;
	}
	knobUi.link = { a: knobUi.linkPendingId, b: id };
	knobUi.linkPendingId = null;
}

export function clearKnobLink(): void {
	knobUi.link = null;
	knobUi.linkPendingId = null;
}

/** Apply a delta to a knob; if linked, move the partner inversely with stagger. */
export function nudgeKnob(id: string, delta: number): void {
	if (!Number.isFinite(delta) || delta === 0) return;
	const primary = _activeRef(id);
	if (primary === undefined) return;
	const partnerId = _partnerOf(id);
	if (partnerId === null) {
		primary.setValue(clamp01(primary.getValue() + delta));
		return;
	}
	const secondary = _activeRef(partnerId);
	if (secondary === undefined) {
		primary.setValue(clamp01(primary.getValue() + delta));
		return;
	}
	const next = applyLinkedDelta(primary.getValue(), secondary.getValue(), delta);
	primary.setValue(next.primary);
	secondary.setValue(next.secondary);
}

/** Set absolute value on primary; derive delta vs current for link math. */
export function setKnobAbsolute(id: string, value: number): void {
	const primary = _activeRef(id);
	if (primary === undefined) return;
	const delta = clamp01(value) - primary.getValue();
	nudgeKnob(id, delta);
}

/**
 * Drag helper: recompute from pointer-down baselines so stagger does not
 * accumulate across pointermove frames.
 */
export function setKnobFromDrag(
	id: string,
	startPrimary: number,
	targetPrimary: number,
	partnerId: string | null,
	startSecondary: number | null
): void {
	const primary = _activeRef(id);
	if (primary === undefined) return;
	const target = clamp01(targetPrimary);
	if (partnerId === null || startSecondary === null) {
		primary.setValue(target);
		return;
	}
	const secondary = _activeRef(partnerId);
	if (secondary === undefined) {
		primary.setValue(target);
		return;
	}
	const delta = target - startPrimary;
	const next = applyLinkedDelta(startPrimary, startSecondary, delta);
	primary.setValue(next.primary);
	secondary.setValue(next.secondary);
}

/** Registered selected dials and their current values, in selection order. */
function _selectionBaselines(): { ids: string[]; baselines: Record<string, number> } {
	const ids: string[] = [];
	const baselines: Record<string, number> = {};
	for (const id of knobUi.selectedIds) {
		const ref = _activeRef(id);
		if (ref === undefined) continue;
		ids.push(id);
		baselines[id] = ref.getValue();
	}
	// The Alt partner's baseline too, so a group move can drive it inversely.
	for (const id of ids) {
		const partner = _partnerOf(id);
		if (partner === null || partner in baselines) continue;
		const ref = _activeRef(partner);
		if (ref !== undefined) baselines[partner] = ref.getValue();
	}
	return { ids, baselines };
}

function _applyTargets(targets: Readonly<Record<string, number>>): void {
	for (const [id, value] of Object.entries(targets)) _activeRef(id)?.setValue(value);
}

/** Options for a group nudge: who leads, and whether Opt flips one side (R3). */
export interface GroupNudgeOptions {
	/** The control under the pointer; falls back to the first-selected. */
	leadId?: string | null;
	flip?: boolean;
}

/**
 * Common-mode nudge of the whole selection. One selected dial goes through
 * nudgeKnob; with 2+ selected each dial moves by exactly `delta`, clamped (or
 * by -delta on the flipped side when Opt is held, R3). In both cases an
 * Alt-linked partner moves inversely with the stagger.
 */
export function nudgeSelection(delta: number, opts: GroupNudgeOptions = {}): void {
	if (!Number.isFinite(delta) || delta === 0) return;
	const { ids, baselines } = _selectionBaselines();
	if (ids.length === 0) return;
	if (ids.length === 1) {
		nudgeKnob(ids[0], delta);
		return;
	}
	const lead = opts.leadId != null && ids.includes(opts.leadId) ? opts.leadId : ids[0];
	const signs = leadDragSigns(ids, lead, opts.flip === true);
	const targets: Record<string, number> = {};
	for (const id of ids) targets[id] = clamp01(baselines[id] + signs[id] * delta);
	_applyTargets(applyLinkToTargets(ids, baselines, targets, knobUi.link));
}

/**
 * A dial's own scroll / arrow key: when it is part of a 2+ selection the whole
 * selection moves together, led by this dial, otherwise just this dial
 * (link-aware). `flip` is Opt held during the wheel (R3).
 */
export function nudgeKnobOrSelection(id: string, delta: number, flip: boolean = false): void {
	if (isKnobSelected(id) && knobUi.selectedIds.length >= 2) nudgeSelection(delta, { leadId: id, flip });
	else nudgeKnob(id, delta);
}

/**
 * Per-item direction for a group drag or wheel led by `leadId`. Without the
 * flip every selected item follows the lead. With the flip (Opt held, R3):
 * when the group spans two channels, items on the lead's channel follow and
 * the other channel goes opposite; when every item is on one channel, the lead
 * alone follows and the rest go opposite.
 */
export function leadDragSigns(
	selection: readonly string[],
	leadId: string,
	flip: boolean
): Record<string, 1 | -1> {
	const out: Record<string, 1 | -1> = {};
	const leadGroup = knobGroupOf(leadId);
	const oneChannel = selection.every((id) => knobGroupOf(id) === leadGroup);
	for (const id of selection) {
		const follows = !flip || (oneChannel ? id === leadId : knobGroupOf(id) === leadGroup);
		out[id] = follows ? 1 : -1;
	}
	return out;
}

/**
 * Pointer-down snapshot for a group drag; null when it is a single-dial drag.
 * `offsets` accumulate per item, so a modifier change mid-drag (Opt released)
 * changes only the direction of what comes next and never jumps.
 */
export interface SelectionDragStart {
	ids: string[];
	baselines: Record<string, number>;
	/** The control under the pointer: it leads the direction (R2). */
	leadId: string;
	offsets: Record<string, number>;
}

/** Start a group drag led by `id` when it is selected and 2+ registered controls are. */
export function beginSelectionDrag(id: string): SelectionDragStart | null {
	if (!isKnobSelected(id)) return null;
	const start = _selectionBaselines();
	if (start.ids.length < 2) return null;
	const offsets: Record<string, number> = {};
	for (const sid of start.ids) offsets[sid] = 0;
	return { ...start, leadId: id, offsets };
}

/** Pure: targets from baselines plus accumulated offsets, each clamped to 0..1. */
export function groupDragTargets(start: Readonly<SelectionDragStart>): Record<string, number> {
	const out: Record<string, number> = {};
	for (const id of start.ids) out[id] = clamp01(start.baselines[id] + (start.offsets[id] ?? 0));
	return out;
}

/**
 * One drag frame: `delta` (0..1 units, up-positive) is the lead's move for
 * this frame. Every item adds it with its own sign, then targets go through
 * the registry (and so the typed command path), link-aware.
 */
export function stepSelectionDrag(start: SelectionDragStart, delta: number, flip: boolean): void {
	if (!Number.isFinite(delta) || delta === 0) return;
	const signs = leadDragSigns(start.ids, start.leadId, flip);
	for (const id of start.ids) start.offsets[id] = (start.offsets[id] ?? 0) + signs[id] * delta;
	_applyTargets(applyLinkToTargets(start.ids, start.baselines, groupDragTargets(start), knobUi.link));
}

/**
 * One R7 pad frame: `dx` / `dy` (0..1 units, gained by the caller) add to the
 * X and Y items' accumulated offsets, then go through the registry, link-aware.
 */
export function stepPadDrag(start: SelectionDragStart, dx: number, dy: number): void {
	if (!Number.isFinite(dx) || !Number.isFinite(dy) || (dx === 0 && dy === 0)) return;
	const axes = padAxes(start.ids);
	for (const id of start.ids) start.offsets[id] = (start.offsets[id] ?? 0) + (axes.x.includes(id) ? dx : dy);
	_applyTargets(applyLinkToTargets(start.ids, start.baselines, groupDragTargets(start), knobUi.link));
}

/** Pad drag start: the whole 2+ selection, no lead (the pad itself is the pointer). */
export function beginPadDrag(): SelectionDragStart | null {
	const start = _selectionBaselines();
	if (start.ids.length < 2) return null;
	const offsets: Record<string, number> = {};
	for (const id of start.ids) offsets[id] = 0;
	return { ...start, leadId: start.ids[0], offsets };
}

export function linkedPartnerId(id: string): string | null {
	return _partnerOf(id);
}

export function readKnobValue(id: string): number | null {
	return _activeRef(id)?.getValue() ?? null;
}

function _onWindowWheel(e: WheelEvent): void {
	if (!knobUi.selectedIds.some((id) => _registry.has(id))) return;
	// Ignore when a text field owns focus.
	const t = e.target;
	if (t instanceof HTMLElement) {
		const tag = t.tagName;
		if (tag === 'INPUT' || tag === 'TEXTAREA' || t.isContentEditable) return;
	}
	e.preventDefault();
	const dir = e.deltaY > 0 ? -1 : e.deltaY < 0 ? 1 : 0;
	if (dir === 0) return;
	// This listener is the one wheel-adjustable path that does NOT run through
	// use:wheelAdjust, so the trackpad scaling has to be applied explicitly here
	// or the shift-selected dial stays hypersensitive while every other control
	// is calm. Same seam, same constant.
	const step = scaledWheelStep(KNOB_CFG.scrollStep, detectWheelInputKind(e));
	// R3: the lead is the control under the pointer, else the first-selected.
	nudgeSelection(dir * step, { leadId: knobUi.hoveredId, flip: e.altKey });
}

function _ensureGlobalWheel(): void {
	if (_wheelBound || typeof window === 'undefined') return;
	window.addEventListener('wheel', _onWindowWheel, { passive: false });
	_wheelBound = true;
}

function _onWindowKeyDown(e: KeyboardEvent): void {
	if (e.key === 'Escape') clearKnobSelection();
}

function _onWindowPointerDown(e: PointerEvent): void {
	if (e.button !== 0 || knobUi.selectedIds.length === 0) return;
	if (isEmptySpaceTarget(e.target)) clearKnobSelection();
}

function _ensureSelectionClear(): void {
	if (_clearBound || typeof window === 'undefined') return;
	window.addEventListener('keydown', _onWindowKeyDown);
	window.addEventListener('pointerdown', _onWindowPointerDown, { capture: true });
	_clearBound = true;
}

/**
 * Agent-native parity for MIXUX-13: `window.__mdtKnobSelection` drives the same
 * selection set and moves as Shift+click, scroll and the two-axis drag, from
 * outside the ES module scope (matches `__mdtWheelSensitivity`).
 */
export interface KnobSelectionBridge {
	/** The ordered selection, first-selected first. */
	get: () => string[];
	/** Replace the selection; throws on an id with no mounted dial. */
	set: (ids: string[]) => void;
	clear: () => void;
	/** Common-mode delta in 0..1 units, as scroll and arrow keys apply it. */
	nudge: (delta: number) => void;
	/** The R7 pad in px (dx right-positive moves channel A, dy UP-positive channel B), from current values. */
	drag: (dx: number, dy: number) => void;
	/** Current value of a dial, or null when it is not mounted. */
	value: (id: string) => number | null;
}

export function installKnobSelectionGlobal(): void {
	if (typeof window === 'undefined') return;
	const bridge: KnobSelectionBridge = {
		get: () => [...knobUi.selectedIds],
		set: (ids) => {
			if (!Array.isArray(ids)) throw new TypeError('knob selection must be an array of knob ids');
			const missing = ids.filter((id) => !_registry.has(id));
			if (missing.length > 0) throw new Error(`no mounted knob for id(s): ${missing.join(', ')}`);
			setKnobSelection(ids);
		},
		clear: () => clearKnobSelection(),
		nudge: (delta) => {
			if (!Number.isFinite(delta)) throw new RangeError(`knob nudge must be finite, got ${delta}`);
			nudgeSelection(delta);
		},
		drag: (dx, dy) => {
			if (!Number.isFinite(dx) || !Number.isFinite(dy)) {
				throw new RangeError(`knob drag must be finite, got ${dx}, ${dy}`);
			}
			const { ids, baselines } = _selectionBaselines();
			// The same gain and falloff the pointer gets, so drag sensitivity
			// moves both surfaces alike (Sol P1, PR #6829).
			const gained = gainedStraightDrag(dx, dy);
			if (ids.length >= 2) {
				const unit = KNOB_CFG.dragVerticalPx;
				const targets = padDragTargets(ids, baselines, gained.dx / unit, gained.dy / unit) ?? {};
				_applyTargets(applyLinkToTargets(ids, baselines, targets, knobUi.link));
			} else if (ids.length === 1) {
				nudgeKnob(ids[0], singleDragDelta(gained.dx, gained.dy));
			}
		},
		value: (id) => readKnobValue(id)
	};
	(window as Window & { __mdtKnobSelection?: KnobSelectionBridge }).__mdtKnobSelection = bridge;
}

installKnobSelectionGlobal();
