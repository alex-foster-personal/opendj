/** User-facing message for a rejected `invoke()` (or similar unknown throw). */
export function tauriInvokeErrorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
