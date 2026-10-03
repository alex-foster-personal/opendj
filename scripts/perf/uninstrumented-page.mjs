/**
 * A Chromium page the capture drives WITHOUT Playwright's page instrumentation
 * (PERFMODE-15 leak investigation, Thu 1 Oct 2026).
 *
 * Why: every page Playwright creates gets `Network.enable` with Chromium's
 * default buffers, so the renderer's DevTools network agent keeps a copy of
 * each response body (up to about 200 MB in total) for `Network.getResponseBody`.
 * In Trackify that is every track's audio file, about 9 MB a track, so a 1 h
 * footprint capture measured the agent's buffer filling up, not the app.
 * Measured on silver at 4f8c670f029 over 24 skipped loads with the deck
 * unloaded at each checkpoint: Blink's buffer partition grew 75 -> 180 -> 196 MB
 * with `Network.enable`, and stayed at 7-10 MB without it.
 *
 * How: the page lives in a browser context created over Playwright's
 * BROWSER-level CDP session. Playwright detaches from any target in a context
 * it did not create, so no domain is enabled on this page at all (`Runtime.evaluate`
 * and `Page.navigate` need none), and `send` refuses the `Network` domain outright. The page API is the
 * subset `mode_ratio_browser.mjs` and `trackify-playback-watch.mjs` use, with
 * Playwright's semantics: `evaluate(fn, arg)` runs a self-contained function,
 * `waitForFunction` polls it until truthy.
 */

const POLL_MS = 100;

/** Internal sentinel: an in-flight `Runtime.evaluate` lost the race to `waitForFunction`'s deadline. */
const _EVALUATION_DEADLINE = 'uninstrumented-page evaluation deadline';

/** The domains this page may enable. `Network` is excluded by construction. */
export function assertUninstrumentedMethod(method) {
	if (typeof method !== 'string' || method.startsWith('Network.')) {
		throw new Error(
			`uninstrumented page refuses ${method}: enabling the Network domain makes the renderer ` +
				'buffer every response body, which the PERFMODE-15 footprint capture would then measure'
		);
	}
}

/** A command the protocol itself failed (its `error` field), as opposed to an
 * exception thrown by the page code `evaluate` ran. */
export class CdpProtocolError extends Error {
	constructor(method, protocolMessage) {
		super(`${method}: ${protocolMessage}`);
		this.name = 'CdpProtocolError';
		this.protocolMessage = protocolMessage;
	}
}

/** Chromium's protocol messages for an evaluation that raced a navigation. */
const _CONTEXT_LOSS_MESSAGES = [
	'Execution context was destroyed',
	'Cannot find context with specified id',
	'Cannot find default execution context'
];

/** True only for a protocol-level context loss; never for a page exception,
 * whatever its message says (an AudioContext error mentions "context" too). */
export function isContextLossError(error) {
	return (
		error instanceof CdpProtocolError &&
		_CONTEXT_LOSS_MESSAGES.some((text) => error.protocolMessage.includes(text))
	);
}

function _expression(fn, arg) {
	if (typeof fn !== 'function') throw new TypeError('evaluate expects a function');
	return `(${fn.toString()})(${arg === undefined ? '' : JSON.stringify(arg)})`;
}

function _exceptionText(details) {
	return details.exception?.description ?? details.text ?? 'evaluation threw';
}

/**
 * Opens an about:blank page in a fresh, isolated browser context of `browser`
 * (a Playwright Browser) and returns a page handle over a raw target session.
 */
export async function openUninstrumentedPage(browser) {
	const browserSession = await browser.newBrowserCDPSession();
	const { browserContextId } = await browserSession.send('Target.createBrowserContext', {
		disposeOnDetach: true
	});
	const { targetId } = await browserSession.send('Target.createTarget', {
		url: 'about:blank',
		browserContextId
	});
	const { sessionId } = await browserSession.send('Target.attachToTarget', {
		targetId,
		flatten: false
	});
	let nextId = 0;
	const pending = new Map();
	const sentMethods = [];
	// Codex P1 r4171125805, PR #4888: a target that crashes or detaches never
	// answers its in-flight commands, and `evaluate` blocked on one would hang
	// the capture past every timeout. Once gone, every pending and later
	// command rejects with the reason instead.
	let goneReason = null;
	function _targetGone(reason) {
		if (goneReason !== null) return;
		goneReason = reason;
		for (const waiter of pending.values()) waiter.reject(new Error(`${waiter.method}: ${reason}`));
		pending.clear();
	}
	browserSession.on('Target.detachedFromTarget', (event) => {
		if (event.sessionId === sessionId) _targetGone('the page target detached');
	});
	browser.on('disconnected', () => _targetGone('the browser disconnected'));
	browserSession.on('Target.receivedMessageFromTarget', (event) => {
		if (event.sessionId !== sessionId) return;
		const message = JSON.parse(event.message);
		if (message.method === 'Inspector.targetCrashed') return _targetGone('the page renderer crashed');
		if (message.method === 'Inspector.detached') return _targetGone(`the page inspector detached (${message.params?.reason})`);
		const waiter = message.id === undefined ? undefined : pending.get(message.id);
		if (waiter === undefined) return;
		pending.delete(message.id);
		if (message.error !== undefined) {
			waiter.reject(new CdpProtocolError(waiter.method, message.error.message));
		} else {
			waiter.resolve(message.result);
		}
	});

	async function send(method, params = {}) {
		assertUninstrumentedMethod(method);
		if (goneReason !== null) throw new Error(`${method}: ${goneReason}`);
		sentMethods.push(method);
		const id = ++nextId;
		const result = new Promise((resolve, reject) => pending.set(id, { method, resolve, reject }));
		// Registered before the send so a fast reply is never missed. The caller
		// still gets every rejection through `return result` below; this handler
		// only stops Node from reporting a detach that lands mid-send as unhandled.
		result.catch(() => undefined);
		try {
			await browserSession.send('Target.sendMessageToTarget', {
				sessionId,
				message: JSON.stringify({ id, method, params })
			});
		} catch (error) {
			pending.delete(id);
			throw error;
		}
		return result;
	}

	function _runtimeEvaluateParams(fn, arg) {
		return {
			expression: _expression(fn, arg),
			awaitPromise: true,
			returnByValue: true,
			// Playwright's evaluate passes userGesture: true, which is what lets the
			// page's AudioContext start without a click; keep the same semantics.
			userGesture: true
		};
	}

	async function sendWithDeadline(method, params, remainingMs) {
		assertUninstrumentedMethod(method);
		if (goneReason !== null) throw new Error(`${method}: ${goneReason}`);
		if (remainingMs <= 0) throw new Error(_EVALUATION_DEADLINE);
		sentMethods.push(method);
		const id = ++nextId;
		let timer;
		const result = new Promise((resolve, reject) => pending.set(id, { method, resolve, reject }));
		result.catch(() => undefined);
		try {
			await browserSession.send('Target.sendMessageToTarget', {
				sessionId,
				message: JSON.stringify({ id, method, params })
			});
		} catch (error) {
			pending.delete(id);
			throw error;
		}
		try {
			return await Promise.race([
				result,
				new Promise((_, reject) => {
					timer = setTimeout(() => {
						const waiter = pending.get(id);
						if (waiter !== undefined) {
							pending.delete(id);
							waiter.reject(new Error(_EVALUATION_DEADLINE));
						}
						reject(new Error(_EVALUATION_DEADLINE));
					}, remainingMs);
				})
			]);
		} finally {
			if (timer !== undefined) clearTimeout(timer);
		}
	}

	async function evaluate(fn, arg) {
		const { result, exceptionDetails } = await send('Runtime.evaluate', _runtimeEvaluateParams(fn, arg));
		if (exceptionDetails !== undefined) throw new Error(_exceptionText(exceptionDetails));
		return result.value;
	}

	async function evaluateWithDeadline(fn, arg, remainingMs) {
		const { result, exceptionDetails } = await sendWithDeadline(
			'Runtime.evaluate',
			_runtimeEvaluateParams(fn, arg),
			remainingMs
		);
		if (exceptionDetails !== undefined) throw new Error(_exceptionText(exceptionDetails));
		return result.value;
	}

	function _waitForFunctionTimeoutError(fn, timeout) {
		return new Error(`waitForFunction timed out after ${timeout}ms: ${fn.toString().slice(0, 160)}`);
	}

	async function waitForFunction(fn, arg, { timeout = 30_000 } = {}) {
		const deadline = Date.now() + timeout;
		for (;;) {
			const remaining = deadline - Date.now();
			if (remaining <= 0) throw _waitForFunctionTimeoutError(fn, timeout);
			let value;
			try {
				value = await evaluateWithDeadline(fn, arg, remaining);
			} catch (error) {
				if (error instanceof Error && error.message === _EVALUATION_DEADLINE) {
					throw _waitForFunctionTimeoutError(fn, timeout);
				}
				// A navigation swaps the execution context under a poll; Playwright
				// retries those too. A throw from the predicate itself propagates.
				if (!isContextLossError(error)) throw error;
			}
			if (value) return value;
			if (Date.now() >= deadline) throw _waitForFunctionTimeoutError(fn, timeout);
			const pollMs = Math.min(POLL_MS, Math.max(0, deadline - Date.now()));
			if (pollMs > 0) await new Promise((resolve) => setTimeout(resolve, pollMs));
		}
	}

	return {
		sentMethods,
		evaluate,
		waitForFunction,
		waitForTimeout: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
		/** Navigates and waits for the document to finish parsing (`domcontentloaded`). */
		async goto(url) {
			const { errorText } = await send('Page.navigate', { url });
			if (errorText !== undefined) throw new Error(`navigation to ${url} failed: ${errorText}`);
			const target = new URL(url);
			await waitForFunction(
				(expected) => location.origin + location.pathname === expected && document.readyState !== 'loading',
				target.origin + target.pathname,
				{ timeout: 60_000 }
			);
		},
		/** Raw CDP on this page, for diagnostics (heap, memory). Network stays refused. */
		send,
		async close() {
			await browserSession.send('Target.disposeBrowserContext', { browserContextId });
			await browserSession.detach();
		}
	};
}
