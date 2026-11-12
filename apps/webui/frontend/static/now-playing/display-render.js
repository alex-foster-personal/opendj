export const VARIANTS = ['poster', 'strip'];
export const DEFAULT_VARIANT = 'poster';
export const RECONNECT_AFTER_MS = 5000;

export const DRIFT_CYCLE = [
	[0, 0],
	[6, -4],
	[-5, 5],
	[8, 0],
	[-4, -6],
	[0, 8],
	[-7, -3],
	[3, -7]
];

function escapeHtml(value) {
	return String(value ?? '')
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;')
		.replaceAll("'", '&#39;');
}

export function resolveVariant(stateVariant, search) {
	const params = new URLSearchParams(search || '');
	const query = params.get('variant');
	if (query === 'poster' || query === 'strip') {
		return query;
	}
	if (stateVariant === 'poster' || stateVariant === 'strip') {
		return stateVariant;
	}
	return DEFAULT_VARIANT;
}

export function normalizeState(raw) {
	if (!raw || typeof raw !== 'object') {
		return { playing: null, next: null, variant: null, title_hidden: false };
	}
	const playing = raw.playing && typeof raw.playing === 'object' ? raw.playing : null;
	const next = raw.next && typeof raw.next === 'object' ? raw.next : null;
	const variant =
		typeof raw.variant === 'string' && (raw.variant === 'poster' || raw.variant === 'strip')
			? raw.variant
			: null;
	const title_hidden = Boolean(raw.title_hidden);
	return { playing, next, variant, title_hidden };
}

export function formatClock(date) {
	const d = date instanceof Date ? date : new Date(date);
	const h = d.getHours();
	const m = d.getMinutes();
	return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

export function driftOffset(nowMs) {
	const n = DRIFT_CYCLE.length;
	const i = Math.floor(nowMs / 60000);
	const idx = ((i % n) + n) % n;
	const pair = DRIFT_CYCLE[idx];
	return { x: pair[0], y: pair[1] };
}

export function driftTransform(offset) {
	return `translate(${offset.x}px, ${offset.y}px)`;
}

export function progressFraction(playing) {
	if (!playing || typeof playing !== 'object') return 0;
	const elapsed = Number(playing.elapsed_s);
	const duration = Number(playing.duration_s);
	if (!Number.isFinite(elapsed) || !Number.isFinite(duration) || duration <= 0) return 0;
	return Math.min(1, Math.max(0, elapsed / duration));
}

export function isReconnecting(nowMs, lastOkMs) {
	return nowMs - lastOkMs > RECONNECT_AFTER_MS;
}

export function safeArtwork(url) {
	if (typeof url !== 'string') return null;
	if (!url.startsWith('/') || url.startsWith('//')) return null;
	return url;
}

export function chips(playing) {
	if (!playing || typeof playing !== 'object') {
		return { key: null, bpm: null };
	}
	const camelot = playing.key_camelot;
	const musical = playing.key_musical;
	let key = null;
	if (typeof camelot === 'string' && camelot.trim() !== '') {
		key = camelot.trim();
	} else if (typeof musical === 'string' && musical.trim() !== '') {
		key = musical.trim();
	}
	let bpm = null;
	const rawBpm = Number(playing.bpm);
	if (Number.isFinite(rawBpm) && rawBpm > 0) {
		bpm = `${Math.round(rawBpm)} BPM`;
	}
	return { key, bpm };
}

export function nextLine(next) {
	if (!next || typeof next !== 'object') return '';
	const title = typeof next.title === 'string' ? next.title.trim() : '';
	if (!title) return '';
	const artist = typeof next.artist === 'string' ? next.artist.trim() : '';
	if (artist) {
		return `Next: ${title}, ${artist}`;
	}
	return `Next: ${title}`;
}

export function shellKey(state, variant) {
	const playing = state.playing;
	if (!playing) {
		return `${variant}|idle`;
	}
	const art = safeArtwork(playing.artwork) ?? '';
	const chip = chips(playing);
	const nl = nextLine(state.next);
	const title = state.title_hidden ? 'ID' : typeof playing.title === 'string' ? playing.title : '';
	const artist = state.title_hidden ? '' : typeof playing.artist === 'string' ? playing.artist : '';
	return [variant, state.title_hidden, title, artist, art, chip.key ?? '', chip.bpm ?? '', nl].join(
		'\0'
	);
}

function renderChips(chip) {
	let html = '';
	if (chip.key) {
		html += `<span class="np__chip" data-np-chip="key">${escapeHtml(chip.key)}</span>`;
	}
	if (chip.bpm) {
		html += `<span class="np__chip" data-np-chip="bpm">${escapeHtml(chip.bpm)}</span>`;
	}
	return html;
}

function renderPlayingCard(state, variant, playing) {
	const hidden = state.title_hidden;
	const titleText = hidden ? 'ID' : escapeHtml(playing.title ?? '');
	const artistText = hidden ? '' : escapeHtml(playing.artist ?? '');
	const art = safeArtwork(playing.artwork);
	const chip = chips(playing);
	const frac = progressFraction(playing);
	const pct = Math.round(frac * 1000) / 10;
	const nl = nextLine(state.next);
	const artImg =
		art != null ? `<img class="np__cover-img" data-np-art src="${escapeHtml(art)}" alt="">` : '';
	const nextBlock = nl ? `<div class="np__next" data-np-next>${escapeHtml(nl)}</div>` : '';
	return `
		<div class="np__card" data-np-card>
			<div class="np__cover"><div class="np__cover-ph"></div>${artImg}</div>
			<div class="np__text">
				<div class="np__title" data-np-title>${titleText}</div>
				<div class="np__artist" data-np-artist>${artistText}</div>
				<div class="np__chips">${renderChips(chip)}</div>
				<div class="np__progress"><div class="np__progress-fill" data-np-progress style="width: ${pct}%"></div></div>
				${nextBlock}
			</div>
		</div>`;
}

function renderIdle() {
	return `
		<div class="np__idle" data-np-idle>
			<div class="np__wordmark">Open DJ</div>
			<div class="np__idle-line">Up next soon</div>
		</div>`;
}

function renderPosterBg(playing) {
	const art = safeArtwork(playing?.artwork);
	if (!art) return '';
	return `<div class="np__bg" aria-hidden="true"><img class="np__bg-img" data-np-art-bg src="${escapeHtml(art)}" alt=""></div>`;
}

export function renderDisplay(state, options) {
	const variant = options.variant ?? DEFAULT_VARIANT;
	const nowMs = options.nowMs ?? 0;
	const reconnecting = Boolean(options.reconnecting);
	const offset = driftOffset(nowMs);
	const transform = driftTransform(offset);
	const clock = formatClock(new Date(nowMs));
	const playing = state.playing;
	const idle = playing == null;
	const classes = [
		'np',
		`np--${variant}`,
		idle ? 'np--idle' : '',
		reconnecting ? 'np--reconnecting' : ''
	]
		.filter(Boolean)
		.join(' ');
	const bg = variant === 'poster' && !idle ? renderPosterBg(playing) : '';
	const body = idle ? renderIdle() : renderPlayingCard(state, variant, playing);
	const reconnectHidden = reconnecting ? '' : ' hidden';
	return `<div class="${classes}" data-np-root data-variant="${escapeHtml(variant)}">
	${bg}
	<div class="np__drift" data-np-drift style="transform: ${transform}">
		<div class="np__clock" data-np-clock>${escapeHtml(clock)}</div>
		${body}
	</div>
	<div class="np__reconnecting" data-np-reconnecting${reconnectHidden}>Reconnecting</div>
</div>`;
}
