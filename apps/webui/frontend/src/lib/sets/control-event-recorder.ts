/**
 * Posts page-owned control-event v2 rows to the recorder service (SETREC-08 stems, SETREC-07 hot cues).
 */
import { ApiError, api } from '$lib/api/client';
import type { CommandSource } from '$lib/rb/deck-stop-log';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import type { DeckId } from '$lib/rb/deck-slots';
import { ControlEventCoalescer } from './control-event-coalescer';
import type { ControlEventV2Draft, ControlEventV2Wire, SampleStamp } from './control-event-types';
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

function _enqueuePost(events: ControlEventV2Wire[]): Promise<void> {
	const job = _postChain.then(() => _postImpl(events));
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

export function bindControlEventSession(sessionId: string | null): void {
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
