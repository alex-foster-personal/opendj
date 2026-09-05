// Linkifying an agent's plain-text note (pin 27fe1e3e61b5).
//
// Split out of feedback.ts (Thu 3 Sep 2026, pin review v2): this block is
// fully self-contained (no dependency on anything else in feedback.ts, and
// nothing else in feedback.ts depends on it), so it is the natural extraction
// to bring feedback.ts back under the 600-line file-size gate after the
// followOn draft field (pin b0f-followon) pushed it over.
export interface NoteSegment {
  type: "text" | "link";
  value: string;
}

/** Bare http/https URLs only - never markup. A note is plain text an agent
 * wrote, so turning it into real anchors must never risk innerHTML of
 * untrusted content; this only ever produces text nodes and <a> hrefs built
 * from a URL this same regex already matched. Stops at the first whitespace,
 * which is the same boundary a human reading the note would use. */
const URL_PATTERN = /https?:\/\/\S+/g;

/** Trailing prose punctuation (a period ending a sentence, a comma before
 * "and", a closing paren that belongs to the sentence rather than the URL)
 * is not part of the link - it is moved back into the surrounding text so
 * the href is not silently corrupted. A trailing ")" is only trimmed when
 * it does not close a "(" that is genuinely inside the URL, so a URL whose
 * own path legitimately contains balanced parens is left alone. */
const ALWAYS_TRAILING_PUNCTUATION = new Set([".", ",", ";", ":", "!", "?", "'", '"']);

function splitTrailingPunctuation(url: string): { core: string; trailing: string } {
  let core = url;
  let trailing = "";
  while (core.length > 0) {
    const last = core[core.length - 1];
    if (ALWAYS_TRAILING_PUNCTUATION.has(last)) {
      trailing = last + trailing;
      core = core.slice(0, -1);
      continue;
    }
    if (last === ")") {
      const opens = (core.match(/\(/g) ?? []).length;
      const closes = (core.match(/\)/g) ?? []).length;
      if (closes > opens) {
        trailing = last + trailing;
        core = core.slice(0, -1);
        continue;
      }
    }
    break;
  }
  return { core, trailing };
}

export function linkifyAgentNote(text: string): NoteSegment[] {
  if (text === "") return [];
  const segments: NoteSegment[] = [];
  let lastIndex = 0;
  for (const match of text.matchAll(URL_PATTERN)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      segments.push({ type: "text", value: text.slice(lastIndex, start) });
    }
    const { core } = splitTrailingPunctuation(match[0]);
    segments.push({ type: "link", value: core });
    lastIndex = start + core.length;
  }
  if (lastIndex < text.length) {
    segments.push({ type: "text", value: text.slice(lastIndex) });
  }
  return segments;
}
