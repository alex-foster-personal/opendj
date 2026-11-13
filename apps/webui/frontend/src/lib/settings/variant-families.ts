/**
 * Every taste-variant family, imported for its `defineVariantSetting` side effect (#13727).
 *
 * `catalog.ts` imports this index once, so each family's rows join `VARIANT_REGISTRY`
 * before the catalogue spreads it. A new family adds one import line here and never
 * touches `catalog.ts`. Import the family's def-only `*-variants.ts` module, never its
 * kernel, so no DSP joins the settings chunk. Def modules import only `variant-setting`,
 * never `catalog` or `prefs`, so the index adds no import cycle.
 */
import '$lib/assistant/oj-agent-mood';
import '$lib/components/assistant/oj-agent-header';
import '$lib/rb/color-fx-noise-variants';
import '$lib/rb/commanded-move-presentation';
import '$lib/rb/pitch-fx-variants';
import '$lib/rb/stutter-fx-variants';
