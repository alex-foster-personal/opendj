import type { SettingDef } from "./catalog-types";

// STRUCT-05. Split out of catalog.ts, which sits at the frontend file-size gate.
export const SHOW_SECTIONS_SETTING: SettingDef = {
  id: "show_sections",
  label: "Show sections",
  group: "performance",
  keywords: [
    "section",
    "sections",
    "phrase",
    "phrases",
    "segment",
    "segments",
    "structure",
    "intro",
    "drop",
    "breakdown",
    "outro",
    "overlay",
  ],
  title:
    "Paint analyzed sections as labeled bands over the deck and main waveforms",
  detail:
    "When on, each analyzed section (rekordbox phrase or own section, per the Phrase source choice) paints as a translucent colored band with its label over the main scrolling waveform, the deck overview strip and the browser preview. Default off. Also on the Phrase row of the analysis-source menu.",
  implemented: true,
  control: { kind: "boolean" },
};
