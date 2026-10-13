"""The genre taxonomy Open DJ infers into: Beatport's 46 genre names plus one custom genre.

The owner chose Beatport's list on Mon 12 Oct 2026 ("we'll use their list. It's
better."). The names are Beatport's; every one-line description is our own wording
of what belongs in the class. ``Classical Remixed (custom)`` is the owner's addition
for classical pieces reworked as club tracks.

The instructions and descriptions are the frozen prompt calibrated in
``(private path)`` (rounds 5 to 11, ``v3-baseline``). Changing a
word changes every answer: bump :data:`TAXONOMY_VERSION` so cached findings are
recomputed instead of silently mixed with answers to a different question.

-Claude
"""

from __future__ import annotations

TAXONOMY_VERSION: str = "beatport46+custom/v1"

INSTRUCTIONS: tuple[str, ...] = (
    "The STATE describes one track in a DJ's music library: artist, title (with mix name), album, record label, year, tempo, key, comments and file name, whichever are known.",  # noqa: E501 (frozen prompt text)
    "Which Beatport genre would this track be filed under?",
    "Use what you know about the artist, remixer and record label first; the mix name (for example Extended Mix, Remix by a known producer) and the tempo help.",  # noqa: E501 (frozen prompt text)
    "A remix takes the genre of the remixer's style, not the original song's.",
    "Dance / Pop is vocal radio-style dance music; Pop, Rock, Hip-Hop and R&B are songs that are not dance tracks.",
    "If the STATE includes web search results, a store genre for this exact track (Beatport first, then Traxsource or Juno) is the strongest evidence; magazine or streaming descriptions are weaker.",  # noqa: E501 (frozen prompt text)
)
GENRES: dict[str, str] = {
    "Drum & Bass": "fast breakbeat dance music around 170 to 176 BPM with heavy sub bass: liquid, neurofunk, jump up, rollers, jungle",
    "Hard Techno": "fast, distorted, pounding techno around 145 to 160 BPM, industrial and rave sounds",
    "Electronica": "electronic listening music, often melodic or experimental, not built for the peak of a club set",
    "House": "four-on-the-floor house around 118 to 128 BPM: classic, vocal, soulful, piano or chicago house",
    "Techno (Peak Time / Driving)": "big-room driving techno around 128 to 140 BPM for peak-time club and festival sets",
    "Trance (Main Floor)": "uplifting, euphoric or progressive trance around 132 to 140 BPM with big breakdowns and anthemic leads",
    "Hard Dance / Hardcore / Neo Rave": "hardstyle, hardcore, gabber, hard house and neo rave, 150 BPM and up",
    "Breaks / Breakbeat / UK Bass": "breakbeat rhythms rather than four-on-the-floor: breaks, nu skool breaks, UK bass",
    "Tech House": "groovy, percussive, bass-led house around 124 to 128 BPM, minimal melody, often vocal hooks",
    "Deep House": "warm, soulful, jazzy or dubby house around 118 to 124 BPM with deep chords",
    "Psy-Trance": "psychedelic trance around 138 to 148 BPM with rolling basslines: goa, full on, progressive psy",
    "Minimal / Deep Tech": "stripped-back, hypnotic minimal and deep tech around 122 to 128 BPM",
    "Progressive House": "long, building, melodic progressive house around 120 to 128 BPM",
    "DJ Tools / Acapellas": "acapellas, loops, intros, tools and effects for DJs rather than full songs",
    "Dubstep": "half-time bass music around 140 BPM with heavy wobbles and drops: riddim, brostep, tearout",
    "Indie Dance": "dance music with indie, disco, new wave or alternative influence, often darker or guitar-tinged, around 110 to 125 BPM",
    "Trap / Future Bass": "electronic trap and future bass: half-time beats, rolling hi-hats, big supersaw chords",
    "Dance / Pop": "vocal pop-dance and radio-friendly dance remixes of pop songs, chart dance music",
    "Nu Disco / Disco": "disco, nu disco, edits and boogie with funky basslines and strings, around 110 to 124 BPM",
    "Downtempo": "slow, chilled electronic music under about 110 BPM: chillout, trip hop, lounge",
    "Funky House": "funky, disco-sampled, upbeat house with filtered loops and horns",
    "Bass / Club": "club bass music: jersey club, baltimore club, ghetto house and other bass-heavy club styles",
    "UK Garage / Bassline": "UK garage, 2-step, speed garage and bassline around 130 to 138 BPM with swung beats",
    "Afro House": "house with African percussion, chants and rhythms around 118 to 124 BPM, including afro tech",
    "Melodic House & Techno": "emotive, melodic, atmospheric house and techno around 120 to 126 BPM with big synth arpeggios and long builds",
    "Bass House": "house with gritty, aggressive bass synths and drops around 124 to 128 BPM",
    "Techno (Raw / Deep / Hypnotic)": "underground, dark, hypnotic, raw or dub techno",
    "Organic House": "downtempo-to-midtempo house with organic, acoustic and world instruments, 110 to 122 BPM",
    "Electro (Classic / Detroit / Modern)": "electro: 808 drum machines, robotic synths, broken beats",
    "140 / Deep Dubstep / Grime": "deep dubstep, grime and 140 BPM UK sound-system music",
    "Mainstage": "festival big room, EDM and mainstage anthems with huge drops",
    "Jackin House": "jacking, chicago-style, swung house with chopped vocals",
    "Amapiano": "South African amapiano with log drums, around 110 to 115 BPM",
    "Trance (Raw / Deep / Hypnotic)": "darker, hypnotic, raw or deep trance",
    "Ambient / Experimental": "ambient, drone, experimental or beatless electronic music",
    "African": "African popular music: afrobeats, afropop, highlife",
    "Caribbean": "reggae, dancehall, soca, dub",
    "Country": "country and americana songs",
    "Hip-Hop": "hip hop and rap",
    "Latin": "latin music: reggaeton, salsa, cumbia, latin pop",
    "Pop": "pop songs that are not dance tracks",
    "R&B": "R&B and soul songs",
    "Rock": "rock, indie rock, alternative, metal",
    "DJ Edits": "DJ edits and bootleg re-edits of existing songs",
    "Latin Electronic": "electronic dance music with latin rhythms and vocals: latin house, latin tech",
    "Indie Tech": "indie-leaning, quirky, vocal tech house and techno crossover",
    "Classical Remixed (custom)": "well-known classical or orchestral pieces (often Bach, Beethoven, Mozart, Vivaldi, film-score themes) reworked as club tracks, usually techno or melodic house; titles often name the original piece",  # noqa: E501 (frozen prompt text)
}

NAMES: tuple[str, ...] = tuple(GENRES)


def canonical(name: str) -> str | None:
    """The taxonomy's spelling of ``name`` (case and surrounding space ignored), else None."""
    wanted = name.strip().casefold()
    return next((g for g in NAMES if g.casefold() == wanted), None)


__all__ = ["GENRES", "INSTRUCTIONS", "NAMES", "TAXONOMY_VERSION", "canonical"]
