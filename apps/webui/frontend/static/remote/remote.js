import { REMOTE_TABS, renderTabBar } from './remote-tabs.js';
import { shouldPollRequests } from './requests-tab-render.js';
import {
	fetchRequestsState,
	loadRequestToDeck,
	postRequestAction,
	putRequestSession
} from './requests-tab.js';

const TOKEN_KEY = 'odj_remote_token';
const POLL_MS = 2000;

let pollTimer = null;
let pickingId = null;

function hashCode() {
	const raw = window.location.hash.startsWith('#')
		? window.location.hash.slice(1)
		: window.location.hash;
	return new URLSearchParams(raw).get('code');
}

function storedToken() {
	try {
		return window.sessionStorage.getItem(TOKEN_KEY);
	} catch {
		return null;
	}
}

function saveToken(token) {
	try {
		window.sessionStorage.setItem(TOKEN_KEY, token);
	} catch {
		/* private mode can refuse sessionStorage */
	}
}

function clearToken() {
	try {
		window.sessionStorage.removeItem(TOKEN_KEY);
	} catch {
		/* private mode can refuse sessionStorage */
	}
}

async function pairWithCode(code) {
	const response = await fetch('/remote/pair', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ code, device_name: 'Phone' })
	});
	if (!response.ok) return null;
	const body = await response.json();
	if (body.token) saveToken(body.token);
	return body.token || null;
}

function renderShell(selectedId, inner, badges = {}) {
	return `${renderTabBar(selectedId, badges)}${inner}`;
}

function renderPairForm(message) {
	const note = message ? `<p class="status-line">${message}</p>` : '';
	return `
		<form class="pair-form" data-pair-form>
			<h1>Phone remote</h1>
			${note}
			<label>
				Pairing code
				<input name="code" maxlength="12" autocomplete="one-time-code" required />
			</label>
			<button type="submit">Pair</button>
		</form>
	`;
}

function showPairForm(root, message) {
	stopPolling();
	clearToken();
	root.innerHTML = renderPairForm(message);
	bindPairForm(root);
}

function bindPairForm(root) {
	const form = root.querySelector('[data-pair-form]');
	if (!form) return;
	form.addEventListener('submit', async (event) => {
		event.preventDefault();
		const typed = new FormData(event.target).get('code');
		const token = await pairWithCode(String(typed || ''));
		if (!token) {
			showPairForm(root, 'That code was not accepted');
			return;
		}
		await showTab(root, token, 'requests');
	});
}

function apiFor(token, root) {
	return async (path, options = {}) => {
		const headers = {
			authorization: `Bearer ${token}`,
			...(options.body ? { 'content-type': 'application/json' } : {}),
			...options.headers
		};
		const response = await fetch(path, { ...options, headers });
		if (response.status === 401) {
			showPairForm(root, 'This phone was forgotten. Pair again.');
		}
		return response;
	};
}

function stopPolling() {
	if (pollTimer === null) return;
	clearInterval(pollTimer);
	pollTimer = null;
}

function startPolling(root, token, selectedId) {
	stopPolling();
	pollTimer = setInterval(() => {
		if (!shouldPollRequests(selectedId, document.hidden)) return;
		void showTab(root, token, selectedId);
	}, POLL_MS);
}

async function main() {
	const root = document.getElementById('app');
	if (!root) return;
	let token = storedToken();
	const code = hashCode();
	if (!token && code) token = await pairWithCode(code);
	document.addEventListener('visibilitychange', () => {
		if (!token) return;
		if (document.hidden) stopPolling();
		else startPolling(root, token, 'requests');
	});
	if (!token) {
		showPairForm(root, '');
		return;
	}
	await showTab(root, token, 'requests');
}

async function showTab(root, token, selectedId) {
	const api = apiFor(token, root);
	const tab = REMOTE_TABS.find((row) => row.id === selectedId) || REMOTE_TABS[0];
	let state = { session: { open: false }, requests: [], inlineReason: null, pickingId };
	if (tab.id === 'requests') {
		try {
			state = await fetchRequestsState(api);
			state.pickingId = pickingId;
			state.nowMs = Date.now();
		} catch {
			state = {
				session: { open: false },
				requests: [],
				inlineReason: 'Could not reach the DJ desk',
				pickingId
			};
		}
	}
	const badges = { requests: (state.requests || []).length };
	root.innerHTML = renderShell(tab.id, tab.render(state), badges);
	root.querySelector('.remote-tabs')?.addEventListener('click', (event) => {
		const button = event.target.closest('[data-tab]');
		if (!button) return;
		void showTab(root, token, button.getAttribute('data-tab') || 'requests');
	});
	bindRequestsActions(root, token, api, state);
	if (shouldPollRequests(tab.id, document.hidden)) startPolling(root, token, tab.id);
	else stopPolling();
}

function bindRequestsActions(root, token, api, state) {
	root.querySelector('[data-session-open]')?.addEventListener('click', async () => {
		const response = await putRequestSession(api, { open: true });
		if (!response.ok) {
			const body = await response.json().catch(() => ({}));
			state.inlineReason = body.message || body.error || 'Could not open requests';
			state.pickingId = pickingId;
			const tab = REMOTE_TABS.find((row) => row.id === 'requests');
			root.innerHTML = renderShell('requests', tab.render(state), {
				requests: (state.requests || []).length
			});
			bindRequestsActions(root, token, api, state);
			return;
		}
		await showTab(root, token, 'requests');
	});
	root.querySelector('[data-session-close]')?.addEventListener('click', async () => {
		await putRequestSession(api, { open: false });
		await showTab(root, token, 'requests');
	});
	root.querySelectorAll('[data-dismiss], [data-loaded]').forEach((button) => {
		button.addEventListener('click', async () => {
			const row = button.closest('[data-request-id]');
			const requestId = row?.getAttribute('data-request-id');
			if (!requestId) return;
			const action = button.hasAttribute('data-loaded') ? 'loaded' : 'dismiss';
			await postRequestAction(api, requestId, action);
			await showTab(root, token, 'requests');
		});
	});
	root.querySelectorAll('[data-load]').forEach((button) => {
		button.addEventListener('click', () => {
			const row = button.closest('[data-request-id]');
			pickingId = row?.getAttribute('data-request-id');
			void showTab(root, token, 'requests');
		});
	});
	root.querySelector('[data-picker-cancel]')?.addEventListener('click', () => {
		pickingId = null;
		void showTab(root, token, 'requests');
	});
	root.querySelectorAll('[data-load-deck]').forEach((button) => {
		button.addEventListener('click', async () => {
			const deck = Number(button.getAttribute('data-load-deck'));
			const requestId = root.querySelector('[data-deck-picker]')?.getAttribute('data-request-id');
			const row = (state.requests || []).find((item) => String(item.id) === String(requestId));
			if (!row) return;
			const response = await loadRequestToDeck(api, row, deck);
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				pickingId = requestId;
				state.inlineReason = body.message || body.error || 'Could not load that request';
				state.pickingId = pickingId;
				const tab = REMOTE_TABS.find((item) => item.id === 'requests');
				root.innerHTML = renderShell('requests', tab.render(state), {
					requests: (state.requests || []).length
				});
				bindRequestsActions(root, token, api, state);
				return;
			}
			pickingId = null;
			await showTab(root, token, 'requests');
		});
	});
}

void main();
