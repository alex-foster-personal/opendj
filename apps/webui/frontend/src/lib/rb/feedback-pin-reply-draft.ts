export const PIN_REPLY_DRAFT_KEY = "odj-feedback-pin-reply-draft";

export type PinReplyDraftMap = Record<string, string>;

export function readPinReplyDraft(storage: Storage, pinId: string): string {
  try {
    const raw = storage.getItem(PIN_REPLY_DRAFT_KEY);
    if (raw === null || raw === "") return "";
    const parsed = JSON.parse(raw) as PinReplyDraftMap;
    if (typeof parsed !== "object" || parsed === null) return "";
    const text = parsed[pinId];
    return typeof text === "string" ? text : "";
  } catch {
    return "";
  }
}

export function persistPinReplyDraft(
  storage: Storage,
  pinId: string,
  text: string,
  onError: (err: unknown) => void,
): void {
  try {
    const trimmed = text.trim();
    const raw = storage.getItem(PIN_REPLY_DRAFT_KEY);
    const map: PinReplyDraftMap =
      raw !== null && raw !== "" ? (JSON.parse(raw) as PinReplyDraftMap) : {};
    if (typeof map !== "object" || map === null) {
      throw new Error("pin reply draft map is corrupt");
    }
    if (trimmed === "") {
      delete map[pinId];
    } else {
      map[pinId] = text;
    }
    if (Object.keys(map).length === 0) {
      storage.removeItem(PIN_REPLY_DRAFT_KEY);
    } else {
      storage.setItem(PIN_REPLY_DRAFT_KEY, JSON.stringify(map));
    }
  } catch (err) {
    onError(err);
  }
}

export function clearPinReplyDraft(
  storage: Storage,
  pinId: string,
  onError: (err: unknown) => void,
): void {
  persistPinReplyDraft(storage, pinId, "", onError);
}
