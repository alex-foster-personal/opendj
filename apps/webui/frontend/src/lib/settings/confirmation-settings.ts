/**
 * The Confirmations settings group: every remembered destructive/move prompt
 * choice, in catalog order. Spread into SETTINGS_CATALOG by catalog.ts.
 *
 * The rows themselves live in confirm-drop-mode.ts (LIBUX-32). This module
 * stays the catalog's import so that file is not a second copy of the same ids.
 */

export { CONFIRM_SETTINGS as CONFIRMATION_SETTINGS } from './confirm-drop-mode';
