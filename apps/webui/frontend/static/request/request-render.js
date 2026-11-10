export const CLOSED_MESSAGE = 'The DJ is not taking requests right now';
export const FREE_TEXT_LABEL = 'Say something';
export const FREE_TEXT_MAX = 140;

export function formatCountdown(seconds) {
	const total = Math.max(0, Math.floor(Number(seconds) || 0));
	const mins = Math.floor(total / 60);
	const secs = total % 60;
	return `${mins}:${String(secs).padStart(2, '0')}`;
}

export function filterCatalog(rows, query) {
	const list = Array.isArray(rows) ? rows : [];
	const needle = String(query ?? '')
		.trim()
		.toLowerCase();
	if (!needle) return list;
	return list.filter((row) => {
		const title = String(row?.title ?? '').toLowerCase();
		const artist = String(row?.artist ?? '').toLowerCase();
		return title.includes(needle) || artist.includes(needle);
	});
}

function escapeHtml(value) {
	return String(value ?? '')
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;');
}

function sessionIsClosed(session) {
	return !session || session.open === false;
}

function renderNameForm() {
	return `
		<form class="name-form" data-name-form>
			<label>
				What's your name?
				<input name="guest-name" maxlength="24" required autocomplete="nickname" />
			</label>
			<button type="submit">Continue</button>
		</form>
	`;
}

export function renderCatalogItems(state) {
	const rows = filterCatalog(state.catalog, state.filter);
	const cooling = Number(state.retryAfterS) > 0;
	const countdown = cooling ? formatCountdown(state.retryAfterS) : '';
	return rows
		.map((row) => {
			const id = escapeHtml(row.stable_id);
			const title = escapeHtml(row.title);
			const artist = escapeHtml(row.artist);
			const art = escapeHtml(row.artwork || `/request/api/artwork/${row.stable_id}`);
			const disabled = cooling ? ' disabled' : '';
			const label = cooling
				? `Request <span class="countdown">${escapeHtml(countdown)}</span>`
				: 'Request';
			return `
				<li class="catalog-row">
					<img class="art" loading="lazy" src="${art}" alt="" />
					<div class="meta">
						<span class="title">${title}</span>
						<span class="artist">${artist}</span>
					</div>
					<button type="button" class="request-track" data-stable-id="${id}"${disabled}>${label}</button>
				</li>
			`;
		})
		.join('');
}

function renderCatalog(state) {
	return `
		<label class="filter">
			Search
			<input type="search" data-catalog-filter value="${escapeHtml(state.filter)}" placeholder="Title or artist" autocomplete="off" />
		</label>
		<ul class="catalog" data-catalog-list>${renderCatalogItems(state)}</ul>
	`;
}

function renderFreeText(state) {
	if (!state.session?.free_text_allowed) return '';
	const text = String(state.draftText ?? '');
	const cooling = Number(state.retryAfterS) > 0;
	const disabled = cooling ? ' disabled' : '';
	return `
		<form class="say-something" data-text-form>
			<label>
				${FREE_TEXT_LABEL}
				<textarea name="message" maxlength="${FREE_TEXT_MAX}" data-free-text${disabled}>${escapeHtml(text)}</textarea>
			</label>
			<div class="char-count" data-char-count>${text.length}/${FREE_TEXT_MAX}</div>
			<button type="submit" class="request-text"${disabled}>Send message</button>
		</form>
	`;
}

export function renderRequestItems(state) {
	const rows = Array.isArray(state.requests) ? state.requests : [];
	const voted = new Set((state.votedIds ?? []).map(String));
	return rows
		.map((row) => {
			const id = escapeHtml(row.id);
			const votedHere = voted.has(String(row.id));
			const mark = votedHere ? ' <span class="vote-mark" aria-hidden="true">&#9679;</span>' : '';
			const votedClass = votedHere ? ' vote-btn is-voted' : ' vote-btn';
			const label = row.kind === 'text' ? row.text : `${row.title ?? ''} - ${row.artist ?? ''}`;
			return `
				<li class="request-row">
					<div class="meta">
						<span class="who">${escapeHtml(row.requester_name)}</span>
						<span class="what">${escapeHtml(label)}</span>
						<span class="votes">${escapeHtml(row.votes ?? 0)} votes</span>
					</div>
					<button type="button" class="${votedClass.trim()}" data-request-id="${id}">Vote${mark}</button>
				</li>
			`;
		})
		.join('');
}

function renderRequests(state) {
	return `
		<section class="requests-so-far">
			<h2>Requests so far</h2>
			<ul data-requests-list>${renderRequestItems(state)}</ul>
		</section>
	`;
}

export function renderPage(state = {}) {
	const session = state.session;
	if (sessionIsClosed(session)) {
		return `<p class="closed-message">${CLOSED_MESSAGE}</p>`;
	}
	if (!state.guestName) {
		return renderNameForm();
	}
	const reasonText = state.inlineReason ? escapeHtml(state.inlineReason) : '';
	const reasonHidden = state.inlineReason ? '' : ' hidden';
	return `
		<main class="request-page">
			<h1 data-catalog-name>${escapeHtml(session.catalog_name || 'Request a track')}</h1>
			<p class="inline-reason" data-inline-reason${reasonHidden}>${reasonText}</p>
			${renderCatalog(state)}
			${renderFreeText(state)}
			${renderRequests(state)}
		</main>
	`;
}
