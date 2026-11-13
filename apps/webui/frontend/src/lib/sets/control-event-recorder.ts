/**
 * Posts page-owned control-event v2 rows to the recorder service (SETREC-11827 FX, SETREC-08 stems, SETREC-07 hot cues).
 */
import { ApiError, api } from '$lib/api/client';
import type { PerformanceCommand } from '$lib/rb/performance-ipc-types';
import type { CommandSource } from '$lib/rb/deck-stop-log';
import type { ColorFxSwitchPlan } from '$lib/rb/color-fx';
import { parseColorFxCommand } from '$lib/rb/color-fx';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import type { DeckId } from '$lib/rb/deck-slots';
import { ControlEventCoalescer } from './control-event-coalescer';
import type { ControlEventV2Draft, ControlEventV2Wire, SampleStamp } from './control-event-types';
import {
	eventsFromBeatFxCommand,
	eventsFromBeatFxReleaseCommand,
	eventsFromColorFxCommand,
	type BeatFxCommandLike,
	type BeatFxReleaseCommandLike
} from './fx-control-events';
import { eventsFromHotCueTrigger } from './loop-cue-control-events';
import { eventsFromStemCommand, type StemCaptureCommand } from './stem-control-events';

export const CONTROL_EVENT_POST_PATH = '/api/sets/control-events';

export type SampleClock = () => SampleStamp | null;

let _sessionId: string | null = null;
let _nextSeq = 0;
let _coalescer = new ControlEventCoalescer();
let _sampleClock: SampleClock = () => null;
let _wallClock: () => string = () => new Date().toISOString();
let _postEnabled = true;
let _postChain: Promise<void> = Promise.resolve();

type PostFn = (events: ControlEventV2Wire[]) => Promise<void>;

let _postImpl: PostFn = async (events) => {
	if (events.length === 0 || !_postEnabled || _sessionId === null) return;
	await api.POST(CONTROL_EVENT_POST_PATH, {
		body: {
			session_id: _sessionId,
			events: events as unknown as Record<string, unknown>[]
		}
	});
};

function _resetPostChain(): void {
	_postChain = Promise.resolve();
}

const SEQ_GAP = /seq gap: expected (\d+), got \d+/;

/** The seq the daemon expects next, when `error` is its 422 seq-gap refusal; otherwise null. */
export function expectedSeqFromError(error: unknown): number | null {
	if (!(error instanceof Error)) return null;
	if ((error as { status?: unknown }).status !== 422) return null;
	const match = SEQ_GAP.exec(error.message);
	return match === null ? null : Number(match[1]);
}

/** Post one batch; on a seq gap (page attached to a session that already holds events), renumber from the daemon's expected seq and retry once. */
async function _postReconciled(events: ControlEventV2Wire[]): Promise<void> {
	try {
		await _postImpl(events);
	} catch (error) {
		const expected = expectedSeqFromError(error);
		if (expected === null || events.length === 0 || events[0].seq === expected) throw error;
		const renumbered = events.map((row, index) => ({ ...row, seq: expected + index }));
		_nextSeq = Math.max(_nextSeq, expected + renumbered.length);
		await _postImpl(renumbered);
	}
}

function _enqueuePost(events: ControlEventV2Wire[]): Promise<void> {
	const job = _postChain.then(() => _postReconciled(events));
	_postChain = job.catch(() => {});
	return job;
}

export function installControlEventRecorderForTest(seam: {
	sampleClock?: SampleClock;
	wallClock?: () => string;
	postEnabled?: boolean;
	post?: PostFn;
}): () => void {
	const prevClock = _sampleClock;
	const prevWall = _wallClock;
	const prevPost = _postEnabled;
	const prevPostImpl = _postImpl;
	if (seam.sampleClock !== undefined) _sampleClock = seam.sampleClock;
	if (seam.wallClock !== undefined) _wallClock = seam.wallClock;
	if (seam.postEnabled !== undefined) _postEnabled = seam.postEnabled;
	if (seam.post !== undefined) _postImpl = seam.post;
	return () => {
		_sampleClock = prevClock;
		_wallClock = prevWall;
		_postEnabled = prevPost;
		_postImpl = prevPostImpl;
	};
}

export function resetControlEventRecorderForTest(): void {
	_sessionId = null;
	_nextSeq = 0;
	_coalescer = new ControlEventCoalescer();
	_resetPostChain();
}

export function controlEventRecorderSessionId(): string | null {
	return _sessionId;
}

/** Bind the page to a recording session; re-binding the same session keeps its seq counter and coalescer. */
export function bindControlEventSession(sessionId: string | null): void {
	if (sessionId !== null && sessionId === _sessionId) return;
	_sessionId = sessionId;
	_nextSeq = 0;
	_coalescer = new ControlEventCoalescer();
	_resetPostChain();
}

function _assignSeq(events: ControlEventV2Draft[]): ControlEventV2Wire[] {
	return events.map((row) => ({ ...row, seq: _nextSeq++ }));
}

function _stampOrSkip(): SampleStamp | null {
	return _sampleClock();
}

async function _post(events: ControlEventV2Wire[]): Promise<void> {
	if (events.length === 0 || !_postEnabled || _sessionId === null) return;
	await _enqueuePost(events);
}

export async function flushControlEvents(nowMs: number, force = false): Promise<void> {
	const flushed = _coalescer.flush(nowMs, force);
	if (flushed.length === 0) return;
	await _post(flushed);
}

async function _captureAndPost(raw: ControlEventV2Draft[], nowMs: number): Promise<void> {
	const events = _assignSeq(raw);
	for (const event of events) _coalescer.push(event, nowMs);
	const due = _coalescer.flush(nowMs, true);
	try {
		await _post(due);
	} catch (error) {
		if (error instanceof ApiError && error.status === 409) {
			bindControlEventSession(null);
			return;
		}
		throw error;
	}
}

export async function captureFxCommand(
	command: PerformanceCommand,
	source: CommandSource,
	plan?: ColorFxSwitchPlan,
	nowMs: number = performance.now()
): Promise<void> {
	if (_sessionId === null) return;
	const stamp = _stampOrSkip();
	if (stamp === null) return;
	const wall = _wallClock();

	let raw: ControlEventV2Draft[] = [];
	if (command.type === 'color_fx' && plan !== undefined) {
		const color = parseColorFxCommand(command as unknown as Record<string, unknown>);
		raw = eventsFromColorFxCommand(color, plan, source, stamp, _sessionId, wall);
	} else if ((command as { type: string }).type === 'beat_fx') {
		raw = eventsFromBeatFxCommand(
			command as unknown as BeatFxCommandLike,
			source,
			stamp,
			_sessionId,
			wall
		);
	} else if ((command as { type: string }).type === 'beat_fx_release') {
		raw = eventsFromBeatFxReleaseCommand(
			command as unknown as BeatFxReleaseCommandLike,
			source,
			stamp,
			_sessionId,
			wall
		);
	} else {
		return;
	}

	await _captureAndPost(raw, nowMs);
}

export async function captureStemCommand(
	command: StemCaptureCommand,
	source: CommandSource,
	nowMs: number = performance.now()
): Promise<void> {
	if (_sessionId === null) return;
	const stamp = _stampOrSkip();
	if (stamp === null) return;
	const wall = _wallClock();

	const raw = eventsFromStemCommand(command, source, stamp, _sessionId, wall);
	await _captureAndPost(raw, nowMs);
}

export async function captureHotCueTrigger(
	command: { deck: DeckId; slot: HotCueSlot },
	cueInMs: number,
	source: CommandSource,
	_nowMs: number = performance.now()
): Promise<void> {
	if (_sessionId === null) return;
	const stamp = _stampOrSkip();
	if (stamp === null) return;
	const wall = _wallClock();
	const raw = eventsFromHotCueTrigger(command, cueInMs, source, stamp, _sessionId, wall);
	const events = _assignSeq(raw);
	try {
		await _post(events);
	} catch (error) {
		if (error instanceof ApiError && error.status === 409) {
			bindControlEventSession(null);
			return;
		}
		throw error;
	}
}
