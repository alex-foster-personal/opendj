/**
 * Posts page-owned control-event v2 rows to the recorder service (SETREC-08 stems).
 */
import { ApiError, api } from '$lib/api/client';
import type { CommandSource } from '$lib/rb/deck-stop-log';
import { ControlEventCoalescer } from './control-event-coalescer';
import type { ControlEventV2Draft, ControlEventV2Wire, SampleStamp } from './control-event-types';
import { eventsFromStemCommand, type StemCaptureCommand } from './stem-control-events';

export const CONTROL_EVENT_POST_PATH = '/api/sets/control-events';

export type SampleClock = () => SampleStamp | null;

let _sessionId: string | null = null;
let _nextSeq = 0;
let _coalescer = new ControlEventCoalescer();
let _sampleClock: SampleClock = () => null;
let _wallClock: () => string = () => new Date().toISOString();
let _postEnabled = true;

export function installControlEventRecorderForTest(seam: {
	sampleClock?: SampleClock;
	wallClock?: () => string;
	postEnabled?: boolean;
}): () => void {
	const prevClock = _sampleClock;
	const prevWall = _wallClock;
	const prevPost = _postEnabled;
	if (seam.sampleClock !== undefined) _sampleClock = seam.sampleClock;
	if (seam.wallClock !== undefined) _wallClock = seam.wallClock;
	if (seam.postEnabled !== undefined) _postEnabled = seam.postEnabled;
	return () => {
		_sampleClock = prevClock;
		_wallClock = prevWall;
		_postEnabled = prevPost;
	};
}

export function resetControlEventRecorderForTest(): void {
	_sessionId = null;
	_nextSeq = 0;
	_coalescer = new ControlEventCoalescer();
}

export function controlEventRecorderSessionId(): string | null {
	return _sessionId;
}

export function bindControlEventSession(sessionId: string | null): void {
	_sessionId = sessionId;
	_nextSeq = 0;
	_coalescer = new ControlEventCoalescer();
}

function _assignSeq(events: ControlEventV2Draft[]): ControlEventV2Wire[] {
	return events.map((row) => ({ ...row, seq: _nextSeq++ }));
}

function _stampOrSkip(): SampleStamp | null {
	return _sampleClock();
}

async function _post(events: ControlEventV2Wire[]): Promise<void> {
	if (events.length === 0 || !_postEnabled || _sessionId === null) return;
	await api.POST(CONTROL_EVENT_POST_PATH, {
		body: {
			session_id: _sessionId,
			events: events as unknown as Record<string, unknown>[]
		}
	});
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
