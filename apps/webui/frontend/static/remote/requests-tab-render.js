export function requestLabel(row) {
	if (!row) return '';
	if (row.kind === 'text') return String(row.text ?? '');
	const title = String(row.title ?? '').trim();
	const artist = String(row.artist ?? '').trim();
	if (title && artist) return `${title} - ${artist}`;
	return title || artist || String(row.stable_id ?? '');
}

export function formatAge(createdAt, nowMs = Date.now()) {
	const then = Date.parse(createdAt);
	if (Number.isNaN(then)) return '';
	const seconds = Math.max(0, Math.floor((nowMs - then) / 1000));
	if (seconds < 60) return `${seconds}s ago`;
	const minutes = Math.floor(seconds / 60);
	if (minutes < 60) return `${minutes}m ago`;
	return `${Math.floor(minutes / 60)}h ago`;
}

export function shouldPollRequests(selectedId, hidden) {
	return selectedId === 'requests' && hidden !== true;
}

function escapeHtml(value) {
	return String(value ?? '')
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;');
}

function sessionIsOpen(session) {
	return Boolean(session && session.open === true);
}

export function renderDeckPicker(requestId) {
	const id = escapeHtml(requestId);
	const decks = [1, 2, 3, 4]
		.map((deck) => `<button type="button" data-load-deck="${deck}">Deck ${deck}</button>`)
		.join('');
	return `
		<div class="deck-picker" data-deck-picker data-request-id="${id}">
			<p>Load onto</p>
			<div class="actions">${decks}</div>
			<button type="button" data-picker-cancel>Cancel</button>
		</div>
	`;
}

function renderRow(row, state) {
	const id = escapeHtml(row.id);
	const name = escapeHtml(row.requester_name);
	const label = escapeHtml(requestLabel(row));
	const votes = escapeHtml(row.votes ?? 0);
	const age = escapeHtml(formatAge(row.created_at, state.nowMs));
	const load =
		row.kind === 'track' && row.stable_id ? '<button type="button" data-load>Load</button>' : '';
	return `
		<li class="request-row" data-request-id="${id}">
			<div class="meta">
				<span class="who">${name}</span>
				<span class="what">${label}</span>
				<span class="votes">${votes} votes</span>
				<span class="age">${age}</span>
			</div>
			<div class="actions">
				${load}
				<button type="button" data-dismiss>Dismiss</button>
				<button type="button" data-loaded>Mark loaded</button>
			</div>
		</li>
	`;
}

export function renderRequestsTab(state = {}) {
	const session = state.session || {};
	const open = sessionIsOpen(session);
	const rows = Array.isArray(state.requests) ? state.requests : [];
	const reason = state.inlineReason
		? `<p class="inline-reason">${escapeHtml(state.inlineReason)}</p>`
		: '';
	const empty = rows.length === 0 ? '<p class="empty">No new requests</p>' : '';
	const items = rows.map((row) => renderRow(row, state)).join('');
	const picker = state.pickingId ? renderDeckPicker(state.pickingId) : '';
	return `
		<section class="requests-tab" data-requests-tab>
			<header class="session">
				<h1>Requests</h1>
				<p class="session-state">${open ? 'Open for requests' : 'Not taking requests'}</p>
				<div class="session-actions">
					<button type="button" data-session-open${open ? ' disabled' : ''}>Open</button>
					<button type="button" data-session-close${!open ? ' disabled' : ''}>Close</button>
				</div>
			</header>
			${reason}
			${picker}
			${empty}
			<ul class="request-list">${items}</ul>
		</section>
	`;
}
