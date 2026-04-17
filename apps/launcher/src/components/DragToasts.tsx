import type { DragToast } from "../hooks/useDragEvents";

interface Props {
  toasts: DragToast[];
  onDismiss?: (id: number) => void;
}

/**
 * Lightweight toast stack for drag-lifecycle feedback (UI-REVIEW fix).
 *
 * Purely presentational: `useDragEvents` owns the state + TTL. CSS lives
 * alongside the palette styles in `styles/palette.css` so the whole
 * launcher stays single-file-stylesheet for now (toast lib is overkill
 * for 4 event types). `aria-live="polite"` lets screen readers announce
 * drag outcomes without interrupting the user's current focus.
 */
export default function DragToasts({ toasts, onDismiss }: Props) {
  if (toasts.length === 0) return null;
  return (
    <div className="palette-toasts" aria-live="polite" aria-atomic="false">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`palette-toast palette-toast-${t.kind}`}
          role="status"
          onClick={() => onDismiss?.(t.id)}
        >
          {t.message}
        </div>
      ))}
    </div>
  );
}
