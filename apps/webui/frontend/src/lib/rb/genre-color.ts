/**
 * Rough genre → hue for library hover (only). Known families first;
 * unknown tags stay on the default glow (caller falls back).
 */

const FAMILY: Array<{ match: RegExp; color: string }> = [
	{ match: /\b(drum\s*[&n]\s*bass|dnb|jungle|neurofunk|rollers?)\b/i, color: '#5ee07a' },
	{ match: /\b(breakbeat|breaks|ukg|garage|2[- ]?step)\b/i, color: '#f0c14a' },
	{ match: /\b(techno|industrial|schranz|peak\s*time)\b/i, color: '#5ec8ff' },
	{ match: /\b(tech\s*house)\b/i, color: '#4ad0c8' },
	{ match: /\b(deep\s*house|organic|melodic\s*house)\b/i, color: '#7aa8ff' },
	{ match: /\b(house|afro\s*house|jackin)\b/i, color: '#ff9a4a' },
	{ match: /\b(trance|psytrance|progressive)\b/i, color: '#c084fc' },
	{ match: /\b(hardstyle|hardcore|gabber|frenchcore)\b/i, color: '#ff5a5a' },
	{ match: /\b(dubstep|bass\s*music|riddim|tearout)\b/i, color: '#a78bfa' },
	{ match: /\b(hip[- ]?hop|rap|trap|grime|drill)\b/i, color: '#f472b6' },
	{ match: /\b(disco|nu[- ]?disco|boogie|funk|soul)\b/i, color: '#fb923c' },
	{ match: /\b(ambient|downtempo|chill|lo[- ]?fi)\b/i, color: '#94a3b8' },
	{ match: /\b(electro|edm|big\s*room|festival)\b/i, color: '#38bdf8' },
	{ match: /\b(reggae|dancehall|dub)\b/i, color: '#86efac' },
	{ match: /\b(jazz|latin|afrobeat|world)\b/i, color: '#fbbf24' },
	{ match: /\b(pop|rock|indie|metal)\b/i, color: '#e2e8f0' }
];

/** CSS color for a genre tag, or null when no family matched. */
export function genreHoverColor(tag: string): string | null {
	const t = tag.trim();
	if (t === '') return null;
	for (const { match, color } of FAMILY) {
		if (match.test(t)) return color;
	}
	return null;
}
