import {
	driftOffset,
	driftTransform,
	formatClock,
	isReconnecting,
	normalizeState,
	progressFraction,
	renderDisplay,
	resolveVariant,
	shellKey
} from './display-render.js';

const root = document.getElementById('app');
const override = location.search;

let state = normalizeState(null);
let lastOkMs = Date.now();
let paintedKey = '';
let inFlight = false;

function wireCoverErrors() {
	if (!root) return;
	const imgs = root.querySelectorAll('img[data-np-art], img[data-np-art-bg]');
	for (const img of imgs) {
		img.addEventListener('error', () => {
			img.hidden = true;
		});
	}
}

async function poll() {
	if (inFlight) return;
	inFlight = true;
	let controller = null;
	let timer = null;
	try {
		if (typeof AbortController !== 'undefined') {
			controller = new AbortController();
			timer = setTimeout(() => controller.abort(), 4000);
		}
		const init = {
			cache: 'no-store',
			headers: { accept: 'application/json' }
		};
		if (controller) {
			init.signal = controller.signal;
		}
		const res = await fetch('/now-playing/api/state', init);
		if (res.ok) {
			try {
				const raw = await res.json();
				state = normalizeState(raw);
				lastOkMs = Date.now();
			} catch {
				/* keep last state */
			}
		}
	} catch {
		/* keep last state */
	} finally {
		if (timer !== null) clearTimeout(timer);
		inFlight = false;
		paint();
	}
}

function paint() {
	if (!root) return;
	const variant = resolveVariant(state.variant, override);
	const now = Date.now();
	const reconnecting = isReconnecting(now, lastOkMs);
	const key = shellKey(state, variant);
	if (key !== paintedKey) {
		root.innerHTML = renderDisplay(state, { variant, nowMs: now, reconnecting });
		wireCoverErrors();
		paintedKey = key;
		return;
	}
	const clockEl = root.querySelector('[data-np-clock]');
	if (clockEl) {
		clockEl.textContent = formatClock(new Date(now));
	}
	const progressEl = root.querySelector('[data-np-progress]');
	if (progressEl && state.playing) {
		const pct = Math.round(progressFraction(state.playing) * 1000) / 10;
		progressEl.style.width = `${pct}%`;
	}
	const driftEl = root.querySelector('[data-np-drift]');
	if (driftEl) {
		driftEl.style.transform = driftTransform(driftOffset(now));
	}
	const rootEl = root.querySelector('[data-np-root]');
	if (rootEl) {
		rootEl.classList.toggle('np--reconnecting', reconnecting);
	}
	const reconnectEl = root.querySelector('[data-np-reconnecting]');
	if (reconnectEl) {
		reconnectEl.hidden = !reconnecting;
	}
}

setInterval(poll, 1000);
setInterval(paint, 1000);
void poll();
