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
	browserSession.on('Target.receivedMessageFromTarget', (event) => {
		if (event.sessionId !== sessionId) return;
		const message = JSON.parse(event.message);
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
		sentMethods.push(method);
		const id = ++nextId;
		const result = new Promise((resolve, reject) => pending.set(id, { method, resolve, reject }));
		await browserSession.send('Target.sendMessageToTarget', {
			sessionId,
			message: JSON.stringify({ id, method, params })
		});
		return result;
	}

	async function evaluate(fn, arg) {
		const { result, exceptionDetails } = await send('Runtime.evaluate', {
			expression: _expression(fn, arg),
			awaitPromise: true,
			returnByValue: true,
			// Playwright's evaluate passes userGesture: true, which is what lets the
			// page's AudioContext start without a click; keep the same semantics.
			userGesture: true
		});
		if (exceptionDetails !== undefined) throw new Error(_exceptionText(exceptionDetails));
		return result.value;
	}

	async function waitForFunction(fn, arg, { timeout = 30_000 } = {}) {
		const deadline = Date.now() + timeout;
		for (;;) {
			let value;
			try {
				value = await evaluate(fn, arg);
			} catch (error) {
				// A navigation swaps the execution context under a poll; Playwright
				// retries those too. A throw from the predicate itself propagates.
				if (!isContextLossError(error)) throw error;
			}
			if (value) return value;
			if (Date.now() >= deadline) {
				throw new Error(`waitForFunction timed out after ${timeout}ms: ${fn.toString().slice(0, 160)}`);
			}
			await new Promise((resolve) => setTimeout(resolve, POLL_MS));
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
