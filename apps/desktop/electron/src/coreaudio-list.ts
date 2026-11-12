import { coreAudioOutputDevices } from './output-health';

/** Classified CoreAudio output-list failures for `opendj:list-coreaudio-outputs` (issue #13159). */
export type CoreAudioListFailureCode =
	| 'helper_missing'
	| 'helper_failed'
	| 'no_output_device'
	| 'invalid_json'
	| 'not_array';

export type CoreAudioListFailure = {
	code: CoreAudioListFailureCode;
	reason: string;
};

const HELPER_FAILED_PREFIX = 'output probe helper failed: ';

export function classifyCoreAudioListFailure(error: unknown): CoreAudioListFailure {
	const message = error instanceof Error ? error.message : String(error);
	if (/helper not bundled/.test(message)) {
		return { code: 'helper_missing', reason: message };
	}
	if (message.includes('JSON array')) {
		return { code: 'not_array', reason: message };
	}
	if (message.includes('not JSON')) {
		return { code: 'invalid_json', reason: message };
	}
	if (message.startsWith(HELPER_FAILED_PREFIX)) {
		const detail = message.slice(HELPER_FAILED_PREFIX.length);
		if (/no output device|output listing failed/i.test(detail)) {
			return { code: 'no_output_device', reason: detail };
		}
		return { code: 'helper_failed', reason: detail };
	}
	return { code: 'helper_failed', reason: message };
}

export class CoreAudioListError extends Error {
	readonly code: CoreAudioListFailureCode;
	readonly reason: string;

	constructor(failure: CoreAudioListFailure) {
		super(`coreaudio-list:${failure.code}:${failure.reason}`);
		this.name = 'CoreAudioListError';
		this.code = failure.code;
		this.reason = failure.reason;
	}

	static is(value: unknown): value is CoreAudioListError {
		return value instanceof CoreAudioListError;
	}
}

export function resolveListCoreAudioOutputs(payloadDir: string | null, platform = process.platform): unknown[] | null {
	try {
		return coreAudioOutputDevices(payloadDir, platform);
	} catch (error) {
		const failure = classifyCoreAudioListFailure(error);
		throw new CoreAudioListError(failure);
	}
}
