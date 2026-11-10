import {
	FREE_TEXT_MAX,
	renderCatalogItems,
	renderPage,
	renderRequestItems
} from './request-render.js';

const NAME_KEY = 'odj_request_name';
const VOTED_KEY = 'odj_request_votes';
const POLL_MS = 5000;

function readName() {
	try {
		return localStorage.getItem(NAME_KEY) ?? '';
	} catch {
		return '';
	}
}

function writeName(name) {
	localStorage.setItem(NAME_KEY, name);
}

function readVoted() {
	try {
		const raw = JSON.parse(localStorage.getItem(VOTED_KEY) ?? '[]');
		return Array.isArray(raw) ? raw.map(String) : [];
	} catch {
		return [];
	}
}

function writeVoted(ids) {
	localStorage.setItem(VOTED_KEY, JSON.stringify([...ids]));
}

function jsonCode(body) {
	if (!body || typeof body !== 'object') return '';
	if (typeof body.code === 'string') return body.code;
	if (typeof body.detail === 'string') return body.detail;
	if (body.detail && typeof body.detail === 'object' && typeof body.detail.code === 'string') {
		return body.detail.code;
	}
	return '';
}

async function readJson(response) {
	try {
		return await response.json();
	} catch {
		return {};
	}
}

function asList(body, key) {
	if (Array.isArray(body)) return body;
	if (body && Array.isArray(body[key])) return body[key];
	return [];
}

const state = {
	guestName: readName(),
	session: null,
	catalog: [],
	filter: '',
	requests: [],
	votedIds: readVoted(),
	retryAfterS: null,
	inlineReason: null,
	draftText: ''
};

let cooldownTimer = null;
const root = document.getElementById('app');
let paintedMode = null;
let shellKey = null;
let bound = false;

function viewMode() {
	if (state.session === null) return 'loading';
	if (state.session.open === false) return 'closed';
	if (!state.guestName) return 'name';
	return 'main';
}

function mainShellKey() {
	return `${state.guestName}\0${Boolean(state.session?.free_text_allowed)}`;
}

function refreshReason() {
	const slot = root.querySelector('[data-inline-reason]');
	if (!slot) return;
	if (state.inlineReason) {
		slot.hidden = false;
		slot.textContent = state.inlineReason;
	} else {
		slot.hidden = true;
		slot.textContent = '';
	}
}

function refreshHeading() {
	const heading = root.querySelector('[data-catalog-name]');
	if (heading) {
		heading.textContent = state.session?.catalog_name || 'Request a track';
	}
}

function refreshCatalog() {
	const list = root.querySelector('[data-catalog-list]');
	if (list) list.innerHTML = renderCatalogItems(state);
}

function refreshRequests() {
	const list = root.querySelector('[data-requests-list]');
	if (list) list.innerHTML = renderRequestItems(state);
}

function refreshFreeText() {
	const cooling = Number(state.retryAfterS) > 0;
	const box = root.querySelector('[data-free-text]');
	const send = root.querySelector('.request-text');
	const count = root.querySelector('[data-char-count]');
	if (box) box.disabled = cooling;
	if (send) send.disabled = cooling;
	if (count) count.textContent = `${String(state.draftText ?? '').length}/${FREE_TEXT_MAX}`;
}

function refreshMain() {
	refreshHeading();
	refreshReason();
	refreshCatalog();
	refreshFreeText();
	refreshRequests();
}

function paintShell(html, mode, nextShellKey) {
	root.innerHTML = html;
	paintedMode = mode;
	shellKey = nextShellKey;
}

function paint() {
	if (!root) return;
	const mode = viewMode();
	if (mode === 'loading') {
		paintShell('<p class="loading">Loading...</p>', mode, null);
		return;
	}
	if (mode !== 'main') {
		paintShell(renderPage(state), mode, null);
		return;
	}
	const nextKey = mainShellKey();
	if (paintedMode !== 'main' || shellKey !== nextKey) {
		paintShell(renderPage(state), mode, nextKey);
		return;
	}
	refreshMain();
}

function armCooldown(seconds) {
	state.retryAfterS = Number(seconds) || 0;
	if (cooldownTimer !== null) {
		clearInterval(cooldownTimer);
		cooldownTimer = null;
	}
	if (state.retryAfterS <= 0) {
		state.retryAfterS = null;
		paint();
		return;
	}
	cooldownTimer = setInterval(() => {
		state.retryAfterS = (state.retryAfterS ?? 1) - 1;
		if (state.retryAfterS <= 0) {
			state.retryAfterS = null;
			clearInterval(cooldownTimer);
			cooldownTimer = null;
		}
		paint();
	}, 1000);
	paint();
}

async function handleError(response) {
	const body = await readJson(response);
	if (response.status === 423 || jsonCode(body) === 'requests_closed') {
		state.session = { ...(state.session ?? {}), open: false };
		paint();
		return;
	}
	if (response.status === 429) {
		armCooldown(body.retry_after_s);
		return;
	}
	if (response.status === 422) {
		const code = jsonCode(body);
		state.inlineReason = code || 'Request was not accepted';
		paint();
	}
}

async function loadSession() {
	const response = await fetch('/request/api/session');
	const body = await readJson(response);
	if (response.status === 423 || jsonCode(body) === 'requests_closed' || body.open === false) {
		state.session = { open: false };
		return;
	}
	if (!response.ok) {
		state.session = { open: false };
		return;
	}
	state.session = {
		open: true,
		catalog_name: body.catalog_name ?? '',
		free_text_allowed: Boolean(body.free_text_allowed)
	};
}

async function loadCatalog() {
	if (!state.session?.open) return;
	const response = await fetch('/request/api/catalog');
	if (!response.ok) {
		await handleError(response);
		return;
	}
	state.catalog = asList(await readJson(response), 'tracks');
}

async function loadRequests() {
	if (document.visibilityState !== 'visible') return;
	if (!state.session?.open) return;
	const response = await fetch('/request/api/requests');
	if (!response.ok) {
		await handleError(response);
		return;
	}
	state.requests = asList(await readJson(response), 'requests');
	paint();
}

async function submitRequest(payload) {
	state.inlineReason = null;
	const response = await fetch('/request/api/requests', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(payload)
	});
	if (!response.ok) {
		await handleError(response);
		return;
	}
	state.draftText = '';
	const box = root?.querySelector('[data-free-text]');
	if (box) box.value = '';
	await loadRequests();
}

async function submitVote(id) {
	const response = await fetch(`/request/api/requests/${encodeURIComponent(id)}/vote`, {
		method: 'POST'
	});
	if (!response.ok) {
		await handleError(response);
		return;
	}
	if (!state.votedIds.includes(id)) {
		state.votedIds = [...state.votedIds, id];
		writeVoted(state.votedIds);
	}
	await loadRequests();
}

function bind() {
	if (!root || bound) return;
	bound = true;
	root.addEventListener('submit', (event) => {
		const form = event.target;
		if (!(form instanceof HTMLFormElement)) return;
		if (form.matches('[data-name-form]')) {
			event.preventDefault();
			const input = form.querySelector('[name="guest-name"]');
			const name = String(input?.value ?? '').trim();
			if (name.length < 1 || name.length > 24) return;
			writeName(name);
			state.guestName = name;
			void bootstrap();
			return;
		}
		if (form.matches('[data-text-form]')) {
			event.preventDefault();
			const box = form.querySelector('[data-free-text]');
			const text = String(box?.value ?? '').trim();
			if (!text) return;
			state.draftText = text;
			void submitRequest({ text, name: state.guestName });
		}
	});
	root.addEventListener('input', (event) => {
		const el = event.target;
		if (!(el instanceof HTMLElement)) return;
		if (el.matches('[data-catalog-filter]')) {
			state.filter = el.value;
			refreshCatalog();
			return;
		}
		if (el.matches('[data-free-text]')) {
			state.draftText = el.value;
			refreshFreeText();
		}
	});
	root.addEventListener('click', (event) => {
		const target = event.target;
		if (!(target instanceof Element)) return;
		const trackButton = target.closest('.request-track');
		if (trackButton instanceof HTMLButtonElement) {
			if (trackButton.disabled) return;
			void submitRequest({ stable_id: trackButton.dataset.stableId, name: state.guestName });
			return;
		}
		const voteButton = target.closest('.vote-btn');
		if (voteButton instanceof HTMLButtonElement) {
			void submitVote(voteButton.dataset.requestId);
		}
	});
}

async function bootstrap() {
	try {
		await loadSession();
		await loadCatalog();
		await loadRequests();
	} catch {
		state.session = { open: false };
	}
	paint();
}

bind();
paint();
void bootstrap();
setInterval(() => {
	if (document.visibilityState === 'visible') {
		void loadRequests();
	}
}, POLL_MS);
document.addEventListener('visibilitychange', () => {
	if (document.visibilityState === 'visible') {
		void loadRequests();
	}
});
