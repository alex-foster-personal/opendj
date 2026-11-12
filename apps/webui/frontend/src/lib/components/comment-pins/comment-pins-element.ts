/**
 * Browser custom element for `<comment-pins>` (issue #11295).
 */
import { COMMENT_PINS_TAG } from './comment-pins-constants';
import { CommentPinsController } from './comment-pins-controller';
import { threadForPin, type PinExposure } from './comment-pins-ux';
import type { CommentPin } from './comment-pins-api';

function _readAttr(el: HTMLElement, name: string, fallback: string): string {
	const value = el.getAttribute(name);
	return value === null || value === '' ? fallback : value;
}

export class CommentPinsElementImpl extends HTMLElement {
	#controller = new CommentPinsController();

	connectedCallback(): void {
		this.#syncFromAttributes();
		this.#renderMarkers();
	}

	attributeChangedCallback(name: string): void {
		if (
			name === 'api-base' ||
			name === 'feedback-prefix' ||
			name === 'pathname' ||
			name === 'show-badges'
		) {
			this.#syncFromAttributes();
			this.#renderMarkers();
		}
	}

	static get observedAttributes(): string[] {
		return ['api-base', 'feedback-prefix', 'pathname', 'show-badges'];
	}

	get controller(): CommentPinsController {
		return this.#controller;
	}

	get pins(): CommentPin[] {
		return this.#controller.pins;
	}

	set pins(value: CommentPin[]) {
		this.#controller.pins = value;
		this.#renderMarkers();
	}

	get pathname(): string {
		return this.#controller.pathname;
	}

	set pathname(value: string) {
		this.#controller.pathname = value;
		this.#renderMarkers();
	}

	#syncFromAttributes(): void {
		this.#controller.configureFromHost({
			apiBase: _readAttr(this, 'api-base', ''),
			feedbackPrefix: _readAttr(this, 'feedback-prefix', '/api/v1/feedback'),
			pathname: _readAttr(this, 'pathname', '/'),
			showBadges: this.getAttribute('show-badges') === 'true'
		});
	}

	exposureFor(pinId: string): PinExposure | null {
		return this.#controller.exposureFor(pinId);
	}

	thread(pinId: string) {
		return this.#controller.thread(pinId);
	}

	async hydrate(): Promise<void> {
		await this.#controller.hydrate();
		this.#renderMarkers();
		this.dispatchEvent(new CustomEvent('pinschange', { detail: { pins: this.pins } }));
	}

	async dropAt(
		clientX: number,
		clientY: number,
		text: string,
		page: string
	): Promise<CommentPin | null> {
		const created = await this.#controller.dropAt(clientX, clientY, text, page);
		if (created !== null) {
			this.#renderMarkers();
			this.dispatchEvent(new CustomEvent('pinschange', { detail: { pins: this.pins } }));
		}
		return created;
	}

	reply(pinId: string, text: string) {
		return this.#controller.reply(pinId, text);
	}

	archive(pinId: string) {
		return this.#controller.archive(pinId);
	}

	resolvePin(pinId: string, status: 'fixed' | 'merged', issueUrl?: string | null) {
		return this.#controller.resolvePin(pinId, status, issueUrl ?? null);
	}

	attach(pinId: string, file: File) {
		return this.#controller.attach(pinId, file);
	}

	#renderMarkers(): void {
		const host = this.querySelector('[data-comment-pins-layer]') ?? this;
		let layer = host.querySelector('.cp-markers');
		if (layer === null) {
			layer = document.createElement('div');
			layer.className = 'cp-markers';
			layer.setAttribute('data-comment-pins-layer', '');
			host.appendChild(layer);
		}
		layer.replaceChildren();
		for (const { pin, board, exposure, pos } of this.#controller.markerDescriptors()) {
			const btn = document.createElement('button');
			btn.type = 'button';
			btn.className = 'cp-pin';
			if (exposure.collapsed) btn.classList.add('cp-harvested');
			btn.dataset.pinId = pin.id;
			btn.dataset.pinTier = pos.tier;
			btn.dataset.pinHarvested = exposure.harvested ? 'true' : 'false';
			btn.dataset.pinAnchorMoved = exposure.anchorMoved ? 'true' : 'false';
			btn.dataset.pinBadges = board.badges.join(',');
			btn.style.position = 'fixed';
			btn.style.transform = 'translate(-50%, -50%)';
			btn.style.left = pos.tier === 'viewport' ? `${pos.x_pct}%` : `${pos.x}px`;
			btn.style.top = pos.tier === 'viewport' ? `${pos.y_pct}%` : `${pos.y}px`;
			btn.title = pin.text;
			btn.textContent = '◆';
			if (this.#controller.showBadges()) {
				for (const badge of board.badges) {
					const span = document.createElement('span');
					span.className = `cp-badge cp-badge-${badge}`;
					span.textContent = this.#controller.badgeLabel(badge);
					btn.appendChild(span);
				}
			}
			btn.addEventListener('click', () => {
				this.dispatchEvent(
					new CustomEvent('pinopen', { detail: { pin, thread: threadForPin(pin) } })
				);
			});
			layer.appendChild(btn);
		}
	}
}

export function defineCommentPinsElement(): void {
	if (!customElements.get(COMMENT_PINS_TAG)) {
		customElements.define(COMMENT_PINS_TAG, CommentPinsElementImpl);
	}
}
