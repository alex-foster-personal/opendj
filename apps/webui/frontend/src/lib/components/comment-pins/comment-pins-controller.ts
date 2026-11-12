/**
 * Framework-neutral comment-pin state and HTTP (issue #11295); no DOM types.
 */
import { pinFromClient, describeAnchor } from '$lib/rb/feedback';
import {
	capturePinPlacement,
	resolvePinPosition,
	measureSelector
} from '$lib/rb/feedback-pin-position';
import { resolvePinAnchorAt } from '$lib/rb/feedback-pin-placement';
import { pinBoardState } from '$lib/rb/feedback-pin-board';
import {
	CommentPinsApi,
	commentPathsForPrefix,
	type CommentCreateIn,
	type CommentPin
} from './comment-pins-api';
import { anchorMovedForPin, badgeLabel, pinExposure, threadForPin } from './comment-pins-ux';

export class CommentPinsController {
	#api: CommentPinsApi;
	#pins: CommentPin[] = [];
	#pathname = '/';
	#optionBadges = false;

	constructor(api: CommentPinsApi = new CommentPinsApi()) {
		this.#api = api;
	}

	configureFromHost(attrs: {
		apiBase: string;
		feedbackPrefix: string;
		pathname: string;
		showBadges: boolean;
	}): void {
		this.#api = new CommentPinsApi({
			apiBase: attrs.apiBase,
			paths: commentPathsForPrefix(attrs.feedbackPrefix)
		});
		this.#pathname = attrs.pathname;
		this.#optionBadges = attrs.showBadges;
	}

	get api(): CommentPinsApi {
		return this.#api;
	}

	get pins(): CommentPin[] {
		return this.#pins;
	}

	set pins(value: CommentPin[]) {
		this.#pins = value;
	}

	get pathname(): string {
		return this.#pathname;
	}

	set pathname(value: string) {
		this.#pathname = value;
	}

	#elementFromPoint(x: number, y: number) {
		if (typeof document === 'undefined') return null;
		return resolvePinAnchorAt(x, y, (px, py) => document.elementsFromPoint(px, py));
	}

	exposureFor(pinId: string) {
		const pin = this.#pins.find((p) => p.id === pinId);
		if (pin === undefined) return null;
		const moved = anchorMovedForPin(pin, (x, y) => this.#elementFromPoint(x, y));
		return pinExposure(pin, this.#pathname, moved);
	}

	thread(pinId: string) {
		const pin = this.#pins.find((p) => p.id === pinId);
		if (pin === undefined) return [];
		return threadForPin(pin);
	}

	async hydrate(): Promise<CommentPin[]> {
		this.#pins = await this.#api.listComments();
		return this.#pins;
	}

	async dropAt(
		clientX: number,
		clientY: number,
		text: string,
		page: string
	): Promise<CommentPin | null> {
		const vw = typeof window !== 'undefined' ? window.innerWidth : 1000;
		const vh = typeof window !== 'undefined' ? window.innerHeight : 800;
		const point = pinFromClient(clientX, clientY, vw, vh);
		const under = this.#elementFromPoint(clientX, clientY);
		const anchor = describeAnchor(under ?? null);
		const placement = capturePinPlacement(
			{ x: clientX, y: clientY },
			under as Element | null,
			anchor
		);
		const body: CommentCreateIn = {
			text,
			x_pct: point.x_pct,
			y_pct: point.y_pct,
			page,
			anchor,
			author: 'operator',
			ui: 'chrome-loop',
			viewport_width: vw,
			viewport_height: vh,
			element_offset: placement.element_offset,
			nearby_anchors: placement.nearby_anchors
		};
		try {
			const created = await this.#api.createComment(body);
			this.#pins = [...this.#pins, created];
			return created;
		} catch {
			return null;
		}
	}

	async reply(pinId: string, text: string): Promise<CommentPin | null> {
		try {
			const updated = await this.#api.addReply(pinId, text);
			this.#upsert(updated);
			return updated;
		} catch {
			return null;
		}
	}

	async archive(pinId: string): Promise<boolean> {
		try {
			await this.#api.archiveComment(pinId);
			this.#pins = this.#pins.filter((p) => p.id !== pinId);
			return true;
		} catch {
			return false;
		}
	}

	async resolvePin(
		pinId: string,
		status: 'fixed' | 'merged',
		issueUrl: string | null = null
	): Promise<CommentPin | null> {
		try {
			const updated = await this.#api.patchComment(pinId, { status, issue_url: issueUrl });
			this.#upsert(updated);
			return updated;
		} catch {
			return null;
		}
	}

	async attach(pinId: string, file: File): Promise<CommentPin | null> {
		try {
			const updated = await this.#api.uploadAttachment(pinId, file);
			this.#upsert(updated);
			return updated;
		} catch {
			return null;
		}
	}

	#upsert(pin: CommentPin): void {
		const idx = this.#pins.findIndex((p) => p.id === pin.id);
		this.#pins =
			idx === -1 ? [...this.#pins, pin] : this.#pins.map((p, i) => (i === idx ? pin : p));
	}

	/** Marker DOM descriptors for a browser renderer. */
	markerDescriptors(): Array<{
		pin: CommentPin;
		board: ReturnType<typeof pinBoardState>;
		exposure: ReturnType<typeof pinExposure>;
		pos: ReturnType<typeof resolvePinPosition>;
	}> {
		const viewport = {
			w: typeof window !== 'undefined' ? window.innerWidth : 1000,
			h: typeof window !== 'undefined' ? window.innerHeight : 800
		};
		return this.#pins.map((pin) => {
			const moved = anchorMovedForPin(pin, (x, y) => this.#elementFromPoint(x, y));
			const board = pinBoardState(pin, this.#pathname, moved);
			return {
				pin,
				board,
				exposure: pinExposure(pin, this.#pathname, moved),
				pos: resolvePinPosition(pin, measureSelector, viewport)
			};
		});
	}

	showBadges(): boolean {
		return this.#optionBadges;
	}

	badgeLabel = badgeLabel;
}
