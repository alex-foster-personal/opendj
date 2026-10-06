/**
 * Signalsmith stretch worklet readiness: factory import, AudioContext resume,
 * and addModule must complete before the ready handshake timer starts.
 */

import stretchWorkletModuleUrl from '../../../node_modules/signalsmith-stretch/SignalsmithStretch.mjs?url';
import {
	STRETCH_CREATE_TIMEOUT_MS,
	StretchProcessorError,
	withStretchCommandTimeout
} from './stretch-errors';

type CreateSignalsmithStretch = typeof import('signalsmith-stretch').default;

let stretchFactory: Promise<CreateSignalsmithStretch> | null = null;
let stretchImportAttempt = 0;
let addModuleMemos = new WeakMap<AudioContext, Promise<void>>();
let stretchReadyByContext = new WeakMap<AudioContext, Promise<CreateSignalsmithStretch>>();

/** Bug #58: a graph rebuild retry may allow addModule longer than the normal 15 s under load. */
let addModuleTimeoutMs = STRETCH_CREATE_TIMEOUT_MS;
/** Bug #58 fault drill: the next N addModule calls fail as if the module never arrived. */
let injectedAddModuleFailures = 0;

export function setStretchAddModuleTimeoutMs(ms: number): void {
	if (!Number.isFinite(ms) || ms <= 0) throw new Error(`addModule timeout must be a positive number, got ${ms}`);
	addModuleTimeoutMs = ms;
}

export function resetStretchAddModuleTimeoutMs(): void {
	addModuleTimeoutMs = STRETCH_CREATE_TIMEOUT_MS;
}

export function injectStretchAddModuleFailures(count: number): void {
	if (!Number.isInteger(count) || count < 0) throw new Error(`injected addModule failures must be an integer >= 0, got ${count}`);
	injectedAddModuleFailures = count;
}

export function pendingInjectedAddModuleFailures(): number {
	return injectedAddModuleFailures;
}

function _forgetStretchAttempt(attempt: Promise<CreateSignalsmithStretch>): void {
	if (stretchFactory === attempt) {
		stretchImportAttempt += 1;
		stretchFactory = null;
	}
}

function _loadStretchFactory(): Promise<CreateSignalsmithStretch> {
	if (stretchFactory === null) {
		const importSpecifier =
			stretchImportAttempt === 0
				? stretchWorkletModuleUrl
				: `${stretchWorkletModuleUrl}?retry=${stretchImportAttempt}`;
		const attempt: Promise<CreateSignalsmithStretch> = import(/* @vite-ignore */ importSpecifier)
			.then((loaded) => {
				const factory = (loaded as { default?: CreateSignalsmithStretch }).default;
				if (typeof factory !== 'function') {
					throw new StretchProcessorError(
						`${stretchWorkletModuleUrl} did not default-export the Signalsmith factory`
					);
				}
				factory.moduleUrl = stretchWorkletModuleUrl;
				return factory;
			})
			.catch((error) => {
				_forgetStretchAttempt(attempt);
				throw error;
			});
		stretchFactory = attempt;
	}
	return stretchFactory;
}

async function _awaitAddModule(context: AudioContext, moduleUrl: string): Promise<void> {
	let pending = addModuleMemos.get(context);
	if (pending === undefined) {
		pending = (async () => {
			try {
				if (injectedAddModuleFailures > 0) {
					injectedAddModuleFailures -= 1;
					throw new StretchProcessorError('Signalsmith addModule failed: injected fault (bug #58 drill)');
				}
				await withStretchAddModuleTimeout(
					context.audioWorklet!.addModule(moduleUrl),
					addModuleTimeoutMs
				);
			} catch (error) {
				addModuleMemos.delete(context);
				throw error;
			}
		})();
		addModuleMemos.set(context, pending);
	}
	await pending;
}

async function withStretchAddModuleTimeout(
	addModule: Promise<void>,
	timeoutMs: number
): Promise<void> {
	let timer: ReturnType<typeof setTimeout> | undefined;
	try {
		await Promise.race([
			addModule,
			new Promise<never>((_resolve, reject) => {
				timer = setTimeout(
					() =>
						reject(
							new StretchProcessorError(
								`Signalsmith addModule timed out after ${timeoutMs}ms`
							)
						),
					timeoutMs
				);
			})
		]);
	} catch (error) {
		if (error instanceof StretchProcessorError) throw error;
		throw new StretchProcessorError(
			`Signalsmith addModule failed: ${error instanceof Error ? error.message : String(error)}`,
			error instanceof Error ? { cause: error } : undefined
		);
	} finally {
		if (timer !== undefined) clearTimeout(timer);
	}
}

export async function ensureStretchContextRunnable(context: AudioContext): Promise<void> {
	if (context.state === 'suspended' || (context.state as string) === 'interrupted') {
		await context.resume();
	}
	if (context.state !== 'running') {
		throw new StretchProcessorError(
			`AudioContext is ${context.state}; Signalsmith worklet ready handshake cannot run`
		);
	}
}

export async function addStretchWorkletModule(context: AudioContext, moduleUrl: string): Promise<void> {
	await _awaitAddModule(context, moduleUrl);
}

async function _ensureStretchWorkletReadyImpl(
	context: AudioContext
): Promise<CreateSignalsmithStretch> {
	if (context.audioWorklet === undefined) {
		throw new StretchProcessorError('AudioWorklet is unavailable; Signalsmith cannot start');
	}
	const stretchFactoryAttempt = _loadStretchFactory();
	const factory = await withStretchCommandTimeout(
		stretchFactoryAttempt,
		'stretch factory import',
		STRETCH_CREATE_TIMEOUT_MS
	).catch((error) => {
		_forgetStretchAttempt(stretchFactoryAttempt);
		throw error;
	});
	await ensureStretchContextRunnable(context);
	const moduleUrl = factory.moduleUrl ?? stretchWorkletModuleUrl;
	await _awaitAddModule(context, moduleUrl);
	return factory;
}

export async function ensureStretchWorkletReady(
	context: AudioContext
): Promise<CreateSignalsmithStretch> {
	let pending = stretchReadyByContext.get(context);
	if (pending === undefined) {
		pending = _ensureStretchWorkletReadyImpl(context).catch((error) => {
			stretchReadyByContext.delete(context);
			throw error;
		});
		stretchReadyByContext.set(context, pending);
	}
	return pending;
}

export function resetStretchWorkletReadyForTests(): void {
	addModuleTimeoutMs = STRETCH_CREATE_TIMEOUT_MS;
	injectedAddModuleFailures = 0;
	stretchFactory = null;
	stretchImportAttempt = 0;
	addModuleMemos = new WeakMap();
	stretchReadyByContext = new WeakMap();
}
