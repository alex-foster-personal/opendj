//! Scanning kernel for `scripts/oss_tip_audit.py` (the going-public gate).
//!
//! This binary holds NO rules. Python hands it the patterns, flags and prefilter
//! tokens from `scripts/oss_tip_rules.py` (and the known-login pattern from
//! `scripts/oss_tip_logins.py`) and makes every acceptance decision itself. The
//! kernel does only the rule-free, expensive part: read every published blob from
//! `git cat-file --batch`, decode it the way `_decode_text` does, and report each
//! regex match with enough context for Python to re-check it.
//!
//! It mirrors Python's `re` only where it can show it does. Anything else is
//! reported back as UNMIRRORED and Python audits that blob itself:
//! - a UTF-16/UTF-32 byte-order mark, or bytes that are not valid UTF-8;
//! - a character on which the two engines disagree for any single-character atom
//!   of a pattern (Python sends the atoms as PROBEs and compares the answers);
//! - a match that exceeds the backtracking limit.
//!
//! A pattern whose syntax it cannot translate faithfully is refused outright.
//!
//! Protocol: line-oriented commands on stdin, one JSON line per command on stdout.
//! A string is framed as `<byte length>\n<bytes>\n`, an integer as `<n>\n`.
//!
//!   LOAD  <git:str> <root:str> <sniff:int> <n:int> n*<sha:str>
//!     -> {"status":[..]}
//!   PROBE <nprobe:int> nprobe*(<src:str> <flags:int>)
//!     -> {"protocol":1, "chars":[[codepoint, lowercase, "0101.."], ..]} or {"refused":..}
//!   SCAN  <nbad:int> nbad*<cp:int>
//!         <npat:int> npat*(<rule:str> <src:str> <flags:int> <ntok:int> ntok*<tok:str>
//!                          <nbad:int> nbad*<cp:int>)
//!         <n:int> n*<mode:int> <pre:int> <post:int>
//!     -> {"status":[..], "cands":[[blob,pat,line,g0,g1,pre,post,eof],..]} or {"refused":..}
//!   QUIT
//!
//! Status per blob: "b" binary, "t" text, "u:<reason>" unmirrored.
//! Mode per blob: 0 skip, 1 run every pattern.

use fancy_regex::{Expr, Regex};
use std::collections::{BTreeMap, HashSet};
use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
use std::process::{Command, Stdio};
use std::thread;

const BOM_UTF32: [&[u8]; 2] = [&[0xFF, 0xFE, 0x00, 0x00], &[0x00, 0x00, 0xFE, 0xFF]];
const BOM_UTF16: [&[u8]; 2] = [&[0xFF, 0xFE], &[0xFE, 0xFF]];
const BOM_UTF8: &[u8] = &[0xEF, 0xBB, 0xBF];
// Python's re.IGNORECASE and re.UNICODE. Any other flag is refused.
const PY_IGNORECASE: i64 = 2;
const PY_UNICODE: i64 = 32;
// Python's `\w` is `str.isalnum() or "_"`: letters, numbers and the underscore. The
// regex crate's `\w` adds combining marks and connector punctuation and drops some
// numbers, so the Python meaning is spelled out instead. The PROBE handshake still
// checks every character actually present, so a residual difference falls back.
const PY_WORD: &str = r"\p{L}\p{N}_";

struct Text {
    text: String,
    lowered: String,
    ascii_present: u128,
    special: Vec<char>, // sorted distinct non-ASCII characters
}

enum Blob {
    Binary,
    Text(Box<Text>),
    Unmirrored(String),
}

impl Blob {
    fn status(&self) -> String {
        match self {
            Blob::Binary => "b".into(),
            Blob::Text(_) => "t".into(),
            Blob::Unmirrored(reason) => format!("u:{reason}"),
        }
    }
}

struct Input<R: BufRead> {
    inner: R,
}

impl<R: BufRead> Input<R> {
    fn line(&mut self) -> io::Result<Option<String>> {
        let mut buf = String::new();
        if self.inner.read_line(&mut buf)? == 0 {
            return Ok(None);
        }
        Ok(Some(buf.trim_end_matches(['\n', '\r']).to_string()))
    }

    fn int(&mut self) -> io::Result<i64> {
        let line = self.line()?.ok_or_else(|| bad("unexpected end of input"))?;
        line.trim()
            .parse::<i64>()
            .map_err(|_| bad(&format!("not an integer: {line:?}")))
    }

    fn count(&mut self) -> io::Result<usize> {
        usize::try_from(self.int()?).map_err(|_| bad("negative count"))
    }

    fn string(&mut self) -> io::Result<String> {
        let len = self.count()?;
        let mut buf = vec![0u8; len + 1];
        self.inner.read_exact(&mut buf)?;
        if buf.pop() != Some(b'\n') {
            return Err(bad("string frame not terminated by a newline"));
        }
        String::from_utf8(buf).map_err(|_| bad("string frame is not UTF-8"))
    }

    fn chars(&mut self) -> io::Result<HashSet<char>> {
        let n = self.count()?;
        let mut out = HashSet::new();
        for _ in 0..n {
            let cp = u32::try_from(self.int()?).map_err(|_| bad("bad code point"))?;
            out.insert(char::from_u32(cp).ok_or_else(|| bad("bad code point"))?);
        }
        Ok(out)
    }
}

fn bad(msg: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, msg.to_string())
}

fn json_str(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn refused(reasons: &[String]) -> String {
    let mut out = String::from("{\"refused\":");
    json_str(&mut out, &reasons.join("; "));
    out.push('}');
    out
}

/// The worker count. A failure to read the host's parallelism is an error, not a quiet default:
/// the caller reports it, so Python can say UNKNOWN instead of trusting a guessed width.
/// The protocol version every PROBE reply carries. Python refuses any other value, so a stale
/// or overridden kernel fails as UNKNOWN rather than being read against the wrong framing.
const PROTOCOL: u32 = 1;

fn workers() -> Result<usize, String> {
    let parallelism = thread::available_parallelism()
        .map_err(|e| format!("cannot determine worker parallelism: {e}"))?;
    Ok(parallelism.get().clamp(1, 16))
}

// ----- reading and decoding ----------------------------------------------------------

/// Every requested blob, decoded as it arrives, in request order, from one
/// `git cat-file --batch`.
fn read_blobs(git: &str, root: &str, shas: &[String], sniff: usize) -> io::Result<Vec<Blob>> {
    // `git` is the path Python resolved: programs are launched by resolved path.
    let mut child = Command::new(git)
        .args(["-C", root, "cat-file", "--batch"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let mut stdin = child.stdin.take().ok_or_else(|| bad("no stdin for git"))?;
    let request: Vec<u8> = shas
        .iter()
        .flat_map(|s| format!("{s}\n").into_bytes())
        .collect();
    // A writer thread, because git answers while it is still being asked and both
    // pipes would otherwise fill and deadlock.
    let writer = thread::spawn(move || stdin.write_all(&request));
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| bad("no stdout for git"))?;
    let mut stdout = BufReader::with_capacity(1 << 20, stdout);
    let mut out = Vec::with_capacity(shas.len());
    for sha in shas {
        let mut header = String::new();
        stdout.read_line(&mut header)?;
        let fields: Vec<&str> = header.split_whitespace().collect();
        if fields.len() != 3 || fields[0] != sha {
            return Err(bad(&format!("git cat-file answered {header:?} for {sha}")));
        }
        let size: usize = fields[2].parse().map_err(|_| bad("bad object size"))?;
        let mut body = Vec::with_capacity(size + 1);
        (&mut stdout).take(size as u64 + 1).read_to_end(&mut body)?;
        if body.len() != size + 1 || body.pop() != Some(b'\n') {
            return Err(bad(
                "git cat-file object truncated or not followed by a newline",
            ));
        }
        out.push(decode(body, sniff));
    }
    writer
        .join()
        .map_err(|_| bad("git writer thread panicked"))??;
    let mut stderr = String::new();
    if let Some(mut err) = child.stderr.take() {
        err.read_to_string(&mut stderr)?;
    }
    let status = child.wait()?;
    if !status.success() {
        return Err(bad(&format!(
            "git cat-file exited {status}: {}",
            stderr.trim()
        )));
    }
    Ok(out)
}

/// `_decode_text`, for the cases it can reproduce exactly.
fn decode(raw: Vec<u8>, sniff: usize) -> Blob {
    if BOM_UTF32.iter().any(|bom| raw.starts_with(bom)) {
        return Blob::Unmirrored("utf32-bom".into());
    }
    let has_utf8_bom = raw.starts_with(BOM_UTF8);
    if !has_utf8_bom && BOM_UTF16.iter().any(|bom| raw.starts_with(bom)) {
        return Blob::Unmirrored("utf16-bom".into());
    }
    let body = if has_utf8_bom {
        raw[BOM_UTF8.len()..].to_vec()
    } else {
        if raw[..raw.len().min(sniff)].contains(&0) {
            return Blob::Binary;
        }
        raw
    };
    let text = match String::from_utf8(body) {
        Ok(text) => text,
        // Python decodes with errors="replace"; the replacement spans are not
        // something to approximate in a publication gate.
        Err(_) => return Blob::Unmirrored("invalid-utf8".into()),
    };
    let mut seen = [false; 128];
    let mut special = Vec::new();
    if text.is_ascii() {
        for &b in text.as_bytes() {
            seen[b as usize] = true;
        }
    } else {
        for ch in text.chars() {
            if ch.is_ascii() {
                seen[ch as usize] = true;
            } else {
                special.push(ch);
            }
        }
        special.sort_unstable();
        special.dedup();
    }
    let ascii_present =
        seen.iter().enumerate().fold(
            0u128,
            |acc, (i, &on)| if on { acc | (1u128 << i) } else { acc },
        );
    let lowered = if special.is_empty() {
        text.to_ascii_lowercase()
    } else {
        text.to_lowercase()
    };
    Blob::Text(Box::new(Text {
        text,
        lowered,
        ascii_present,
        special,
    }))
}

fn cmd_load<R: BufRead>(input: &mut Input<R>) -> io::Result<(Vec<Blob>, String)> {
    let git = input.string()?;
    let root = input.string()?;
    let sniff = input.count()?;
    let n = input.count()?;
    let mut shas = Vec::with_capacity(n);
    for _ in 0..n {
        shas.push(input.string()?);
    }
    // One git process per worker: decompressing the pack is the floor otherwise.
    // Round-robin, because the big blobs cluster by path.
    let nworkers = workers()
        .map_err(|e| io::Error::new(io::ErrorKind::Other, e))?
        .min(n.max(1));
    let parts: Vec<io::Result<Vec<Blob>>> = thread::scope(|scope| {
        let handles: Vec<_> = (0..nworkers)
            .map(|w| {
                let (git, root) = (&git, &root);
                let part: Vec<String> = shas.iter().skip(w).step_by(nworkers).cloned().collect();
                scope.spawn(move || read_blobs(git, root, &part, sniff))
            })
            .collect();
        handles
            .into_iter()
            .map(|h| h.join().expect("load worker panicked"))
            .collect()
    });
    let mut parts = parts
        .into_iter()
        .map(|p| p.map(Vec::into_iter))
        .collect::<io::Result<Vec<_>>>()?;
    let mut blobs = Vec::with_capacity(n);
    for i in 0..n {
        blobs.push(
            parts[i % nworkers]
                .next()
                .ok_or_else(|| bad("load worker returned too few blobs"))?,
        );
    }
    let mut out = String::from("{\"status\":");
    status_json(&mut out, &blobs);
    out.push('}');
    Ok((blobs, out))
}

// ----- pattern translation -----------------------------------------------------------

/// Python `re` syntax to fancy-regex syntax, or an error for anything whose
/// meaning differs and is not translated here. Refusing is always safe.
fn translate(src: &str, flags: i64) -> Result<String, String> {
    if flags & !(PY_IGNORECASE | PY_UNICODE) != 0 {
        return Err(format!("unsupported flags {flags}"));
    }
    let chars: Vec<char> = src.chars().collect();
    let mut out = String::new();
    if flags & PY_IGNORECASE != 0 {
        out.push_str("(?i)");
    }
    let word_class = format!("[{PY_WORD}]");
    let boundary =
        format!("(?:(?<={word_class})(?!{word_class})|(?<!{word_class})(?={word_class}))");
    let mut i = 0;
    let mut in_class = false;
    let mut class_body = 0usize;
    while i < chars.len() {
        let c = chars[i];
        let next = chars.get(i + 1).copied();
        if c == '\\' {
            let n = next.ok_or("trailing backslash")?;
            match (n, in_class) {
                ('0'..='9', _) => return Err("backreference or octal escape".into()),
                (
                    'N' | 'p' | 'P' | 'g' | 'k' | 'K' | 'z' | 'h' | 'H' | 'e' | 'Q' | 'E' | 'B',
                    _,
                ) => return Err(format!("escape \\{n} is not mirrored")),
                ('Z', false) => out.push_str(r"\z"),
                ('b', true) => return Err("\\b inside a class".into()),
                ('b', false) => out.push_str(&boundary),
                ('w', false) => out.push_str(&word_class),
                ('W', false) => out.push_str(&format!("[^{PY_WORD}]")),
                ('w', true) => out.push_str(PY_WORD),
                ('W', true) => out.push_str(&format!("[^{PY_WORD}]")),
                ('<', _) => out.push_str(r"\x3C"),
                ('>', _) => out.push_str(r"\x3E"),
                _ => {
                    out.push('\\');
                    out.push(n);
                }
            }
            class_body += 1;
            i += 2;
            continue;
        }
        if in_class {
            match c {
                '[' => return Err("unescaped [ inside a class nests in Rust".into()),
                ']' if class_body == 0 => out.push_str(r"\]"),
                ']' => {
                    in_class = false;
                    out.push(']');
                }
                '&' | '-' | '~' if next == Some(c) => {
                    return Err("class set operator in Rust".into())
                }
                _ => out.push(c),
            }
            class_body += 1;
            i += 1;
            continue;
        }
        match c {
            '[' => {
                in_class = true;
                class_body = 0;
                out.push('[');
                if next == Some('^') {
                    out.push('^');
                    i += 1;
                }
            }
            // Python's `$` also matches before a newline that ends the text.
            '$' => out.push_str(r"(?=\n?\z)"),
            '(' if next == Some('?') => {
                let rest: String = chars[i + 2..].iter().take(3).collect();
                let ok = [":", "=", "!", "<=", "<!", "P<", "i)", "i:"];
                let Some(prefix) = ok.iter().find(|p| rest.starts_with(*p)) else {
                    return Err(format!("group syntax (?{rest}"));
                };
                out.push_str("(?");
                out.push_str(prefix);
                i += 2 + prefix.chars().count();
                continue;
            }
            '{' => {
                // A quantifier is copied whole; any other brace is a literal in Python.
                let close = chars[i..].iter().position(|&x| x == '}').map(|p| i + p);
                let body: Option<String> = close.map(|e| chars[i + 1..e].iter().collect());
                let quant = body.as_deref().and_then(|b| {
                    let (lo, hi) = b.split_once(',').unwrap_or((b, ""));
                    let digits = |s: &str| s.chars().all(|d| d.is_ascii_digit());
                    let single = !b.contains(',');
                    (digits(lo) && digits(hi) && !(lo.is_empty() && (single || hi.is_empty())))
                        .then(|| {
                            if lo.is_empty() {
                                format!("0,{hi}")
                            } else {
                                b.to_string()
                            }
                        })
                });
                match (quant, close) {
                    (Some(q), Some(e)) => {
                        out.push('{');
                        out.push_str(&q);
                        out.push('}');
                        i = e + 1;
                        if matches!(chars.get(i), Some('+')) {
                            return Err("possessive quantifier".into());
                        }
                        continue;
                    }
                    _ => out.push_str(r"\{"),
                }
            }
            '}' => out.push_str(r"\}"),
            '*' | '+' | '?' if next == Some('+') => return Err("possessive quantifier".into()),
            _ => out.push(c),
        }
        i += 1;
    }
    if in_class {
        return Err("unterminated class".into());
    }
    Ok(out)
}

// ----- pattern analysis --------------------------------------------------------------

/// The pattern with every lookaround and assertion removed: it matches wherever
/// the pattern does (and more), and the regex crate runs it as a plain automaton.
fn relax(e: &Expr) -> Result<Expr, String> {
    Ok(match e {
        Expr::LookAround(..) | Expr::Assertion(_) => Expr::Empty,
        Expr::Empty | Expr::Any { .. } | Expr::Literal { .. } | Expr::Delegate { .. } => e.clone(),
        Expr::Concat(v) => Expr::Concat(v.iter().map(relax).collect::<Result<_, _>>()?),
        Expr::Alt(v) => Expr::Alt(v.iter().map(relax).collect::<Result<_, _>>()?),
        Expr::Group(c) => Expr::Group(Box::new(relax(c)?)),
        Expr::Repeat {
            child,
            lo,
            hi,
            greedy,
        } => Expr::Repeat {
            child: Box::new(relax(child)?),
            lo: *lo,
            hi: *hi,
            greedy: *greedy,
        },
        other => return Err(format!("cannot relax {other:?}")),
    })
}

/// (min, max) characters consumed; max None when unbounded or unknown.
fn width(e: &Expr) -> (usize, Option<usize>) {
    match e {
        Expr::Empty | Expr::Assertion(_) | Expr::LookAround(..) => (0, Some(0)),
        Expr::Any { .. } => (1, Some(1)),
        Expr::Literal { val, .. } => (val.chars().count(), Some(val.chars().count())),
        Expr::Delegate { size, .. } if *size == 1 => (1, Some(1)),
        Expr::Concat(v) => v.iter().map(width).fold((0, Some(0)), |(a, b), (c, d)| {
            (a + c, b.zip(d).map(|(x, y)| x + y))
        }),
        Expr::Alt(v) => {
            let ws: Vec<_> = v.iter().map(width).collect();
            let lo = ws.iter().map(|w| w.0).min().unwrap_or(0);
            let hi = ws.iter().try_fold(0usize, |acc, w| w.1.map(|x| acc.max(x)));
            (lo, hi)
        }
        Expr::Group(c) => width(c),
        Expr::Repeat { child, lo, hi, .. } => {
            let (cl, ch) = width(child);
            let max = if *hi == usize::MAX {
                None
            } else {
                ch.map(|x| x.saturating_mul(*hi))
            };
            (cl.saturating_mul(*lo), max)
        }
        _ => (0, None),
    }
}

struct Pattern {
    rule: String,
    regex: Regex,
    tokens: Vec<String>,
    bad: HashSet<char>,
    /// The candidate search: the relaxed automaton, and the pattern anchored with
    /// `\G` so an attempt at one position costs only that attempt. None means the
    /// whole text is searched with the real pattern.
    fast: Option<(Regex, Regex)>,
}

fn compile(
    rule: String,
    src: &str,
    flags: i64,
    tokens: Vec<String>,
    bad: HashSet<char>,
) -> Result<Pattern, String> {
    let translated = translate(src, flags)?;
    let regex = Regex::new(&translated).map_err(|e| e.to_string())?;
    let tree = Expr::parse_tree(&translated).map_err(|e| e.to_string())?;
    // Python's finditer and a Rust iterator differ on empty matches; no rule has one.
    if width(&tree.expr).0 == 0 {
        return Err("pattern can match the empty string".into());
    }
    let fast = match relax(&tree.expr) {
        Ok(relaxed) => {
            let mut buf = String::new();
            relaxed.to_str(&mut buf, 0);
            let relaxed = Regex::new(&buf).map_err(|e| e.to_string())?;
            let anchored =
                Regex::new(&format!(r"\G(?:{translated})")).map_err(|e| e.to_string())?;
            Some((relaxed, anchored))
        }
        Err(_) => None,
    };
    Ok(Pattern {
        rule,
        regex,
        tokens,
        bad,
        fast,
    })
}

// ----- probing -----------------------------------------------------------------------

fn cmd_probe<R: BufRead>(input: &mut Input<R>, blobs: &[Blob]) -> io::Result<String> {
    let n = input.count()?;
    let mut probes = Vec::new();
    let mut refusals = Vec::new();
    for _ in 0..n {
        let src = input.string()?;
        let flags = input.int()?;
        match translate(&src, flags)
            .and_then(|t| Regex::new(&format!("^(?:{t})$")).map_err(|e| e.to_string()))
        {
            Ok(re) => probes.push(re),
            Err(e) => refusals.push(format!("probe {src}: {e}")),
        }
    }
    if !refusals.is_empty() {
        return Ok(refused(&refusals));
    }
    let mut ascii = 0u128;
    let mut seen: HashSet<char> = HashSet::new();
    for blob in blobs {
        if let Blob::Text(t) = blob {
            ascii |= t.ascii_present;
            seen.extend(t.special.iter().copied());
        }
    }
    seen.extend(
        (0u32..128)
            .filter(|c| ascii & (1u128 << c) != 0)
            .filter_map(char::from_u32),
    );
    let mut sorted: Vec<char> = seen.into_iter().collect();
    sorted.sort_unstable();
    let mut out = format!("{{\"protocol\":{PROTOCOL},\"chars\":[");
    for (i, ch) in sorted.iter().enumerate() {
        let s = ch.to_string();
        let mut bits = String::with_capacity(probes.len());
        for re in &probes {
            match re.is_match(&s) {
                Ok(true) => bits.push('1'),
                Ok(false) => bits.push('0'),
                // A regex that cannot be evaluated has no answer; it is not a non-match.
                Err(e) => {
                    return Ok(refused(&[format!(
                        "probe match on U+{:04X}: {e}",
                        *ch as u32
                    )]))
                }
            }
        }
        if i > 0 {
            out.push(',');
        }
        out.push_str(&format!("[{},", *ch as u32));
        json_str(&mut out, &ch.to_lowercase().collect::<String>());
        out.push(',');
        json_str(&mut out, &bits);
        out.push(']');
    }
    out.push_str("]}");
    Ok(out)
}

// ----- scanning ----------------------------------------------------------------------

struct Cand {
    blob: usize,
    pat: usize,
    line: usize,
    g0: String,
    g1: Option<String>,
    pre: String,
    post: String,
    eof: bool,
}

fn window_before(text: &str, at: usize, chars: usize) -> &str {
    if chars == 0 {
        return "";
    }
    let start = text[..at]
        .char_indices()
        .rev()
        .nth(chars - 1)
        .map_or(0, |(i, _)| i);
    &text[start..at]
}

fn window_after(text: &str, at: usize, chars: usize) -> (&str, bool) {
    let mut end = at;
    for (taken, (i, ch)) in text[at..].char_indices().enumerate() {
        if taken == chars {
            return (&text[at..at + i], false);
        }
        end = at + i + ch.len_utf8();
        if ch == '\n' {
            return (&text[at..end], end == text.len());
        }
    }
    (&text[at..end], true)
}

type Span = (usize, usize, Option<(usize, usize)>);

/// Every match of the pattern, in the order Python's finditer yields them.
fn find_all(text: &str, p: &Pattern) -> Result<Vec<Span>, String> {
    let err = |e: fancy_regex::Error| format!("regex-error:{e}");
    let mut out = Vec::new();
    let span = |caps: fancy_regex::Captures| -> Result<Span, String> {
        let m = caps.get(0).ok_or("match without group 0")?;
        Ok((
            m.start(),
            m.end(),
            caps.get(1).map(|g| (g.start(), g.end())),
        ))
    };
    let Some((relaxed, anchored)) = &p.fast else {
        for caps in p.regex.captures_iter(text) {
            out.push(span(caps.map_err(err)?)?);
        }
        return Ok(out);
    };
    // The relaxed automaton (every lookaround and assertion removed) matches at
    // every position the pattern does, and it is never empty there. Its
    // leftmost-first, non-overlapping matches therefore COVER every position a
    // match of the pattern can start at: iterating from any point, the next one
    // starts at or before such a position and, being non-empty, either ends past
    // it or the iteration resumes no later than it. So the pattern is tried,
    // anchored, only at positions inside those spans, in order, from where its
    // previous match ended -- which is finditer's own order -- with the whole text
    // as context in both directions.
    let mut resume = 0;
    for m in relaxed.find_iter(text) {
        let m = m.map_err(err)?;
        let mut at = m.start().max(resume);
        while at < m.end() {
            match anchored.captures_from_pos(text, at).map_err(err)? {
                Some(caps) => {
                    let found = span(caps)?;
                    resume = found.1;
                    out.push(found);
                    at = resume;
                }
                None => at += text[at..].chars().next().map_or(1, char::len_utf8),
            }
        }
    }
    Ok(out)
}

/// A rule with no tokens has nothing to rule a blob out with, so it scans every blob.
fn prefilter_hit(tokens: &[String], lowered: &str) -> bool {
    tokens.is_empty() || tokens.iter().any(|tok| lowered.contains(tok.as_str()))
}

fn scan_text(
    blob: usize,
    t: &Text,
    patterns: &[Pattern],
    global_bad: &HashSet<char>,
    pre: usize,
    post: usize,
) -> Result<Vec<Cand>, String> {
    let has_bad = |bad: &HashSet<char>| {
        bad.iter().any(|c| {
            if c.is_ascii() {
                t.ascii_present & (1u128 << (*c as u32)) != 0
            } else {
                t.special.binary_search(c).is_ok()
            }
        })
    };
    if has_bad(global_bad) {
        return Err("char-lowercase".into());
    }
    let mut out = Vec::new();
    let mut line_starts: Option<Vec<usize>> = None;
    for (pat, p) in patterns.iter().enumerate() {
        if !prefilter_hit(&p.tokens, &t.lowered) {
            continue;
        }
        if has_bad(&p.bad) {
            return Err(format!("char-class:{}", p.rule));
        }
        let text = t.text.as_str();
        for (start, end, g1) in find_all(text, p)? {
            let starts = line_starts.get_or_insert_with(|| {
                std::iter::once(0)
                    .chain(text.match_indices('\n').map(|(i, _)| i + 1))
                    .collect()
            });
            let (after, eof) = window_after(text, end, post);
            out.push(Cand {
                blob,
                pat,
                line: starts.partition_point(|&s| s <= start),
                g0: text[start..end].to_string(),
                g1: g1.map(|(a, b)| text[a..b].to_string()),
                pre: window_before(text, start, pre).to_string(),
                post: after.to_string(),
                eof,
            });
        }
    }
    Ok(out)
}

fn cmd_scan<R: BufRead>(input: &mut Input<R>, blobs: &mut [Blob]) -> io::Result<String> {
    let global_bad = input.chars()?;
    let npat = input.count()?;
    let mut patterns = Vec::new();
    let mut refusals = Vec::new();
    for _ in 0..npat {
        let rule = input.string()?;
        let src = input.string()?;
        let flags = input.int()?;
        let ntok = input.count()?;
        let mut tokens = Vec::new();
        for _ in 0..ntok {
            let tok = input.string()?;
            // Only a lowercase ASCII token makes containment in the lowered text
            // independent of context-sensitive lowercasing (final sigma).
            if !tok.is_ascii() || tok.chars().any(|c| c.is_ascii_uppercase()) || tok.is_empty() {
                refusals.push(format!("{rule}: prefilter token is not lowercase ASCII"));
            }
            tokens.push(tok);
        }
        let bad_chars = input.chars()?;
        match compile(rule.clone(), &src, flags, tokens, bad_chars) {
            Ok(p) => patterns.push(p),
            Err(e) => refusals.push(format!("{rule}: {e}")),
        }
    }
    let n = input.count()?;
    if n != blobs.len() {
        return Err(bad("SCAN mode count differs from LOAD"));
    }
    let mut modes = Vec::with_capacity(n);
    for _ in 0..n {
        modes.push(input.int()?);
    }
    let pre = input.count()?;
    let post = input.count()?;
    if !refusals.is_empty() {
        return Ok(refused(&refusals));
    }
    let work: Vec<usize> = (0..n)
        .filter(|&i| modes[i] != 0 && matches!(blobs[i], Blob::Text(_)))
        .collect();
    // An operational failure is fatal, not a refusal: a refusal sends `auto` to the Python backend,
    // which would let CI pass on a kernel that could not run the scan at all.
    let nworkers = workers().map_err(|e| io::Error::new(io::ErrorKind::Other, e))?;
    let results: Vec<(usize, Result<Vec<Cand>, String>)> = thread::scope(|scope| {
        let blobs_ref = &*blobs;
        let patterns_ref = &patterns;
        let global_bad = &global_bad;
        let handles: Vec<_> = (0..nworkers)
            .map(|w| {
                let mine: Vec<usize> = work.iter().copied().skip(w).step_by(nworkers).collect();
                scope.spawn(move || {
                    mine.into_iter()
                        .filter_map(|i| match &blobs_ref[i] {
                            Blob::Text(t) => {
                                Some((i, scan_text(i, t, patterns_ref, global_bad, pre, post)))
                            }
                            _ => None,
                        })
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        handles
            .into_iter()
            .flat_map(|h| h.join().expect("scan worker panicked"))
            .collect()
    });
    let mut by_blob: BTreeMap<usize, Vec<Cand>> = BTreeMap::new();
    for (i, result) in results {
        match result {
            Ok(c) => {
                by_blob.insert(i, c);
            }
            Err(reason) => blobs[i] = Blob::Unmirrored(reason),
        }
    }
    let mut out = String::from("{\"status\":");
    status_json(&mut out, blobs);
    out.push_str(",\"cands\":[");
    let mut first = true;
    for c in by_blob.values().flatten() {
        if !first {
            out.push(',');
        }
        first = false;
        out.push_str(&format!("[{},{},{},", c.blob, c.pat, c.line));
        json_str(&mut out, &c.g0);
        out.push(',');
        match &c.g1 {
            Some(g) => json_str(&mut out, g),
            None => out.push_str("null"),
        }
        out.push(',');
        json_str(&mut out, &c.pre);
        out.push(',');
        json_str(&mut out, &c.post);
        out.push_str(if c.eof { ",1]" } else { ",0]" });
    }
    out.push_str("]}");
    Ok(out)
}

fn status_json(out: &mut String, blobs: &[Blob]) {
    out.push('[');
    for (i, b) in blobs.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        json_str(out, &b.status());
    }
    out.push(']');
}

fn run() -> io::Result<()> {
    let stdin = io::stdin();
    let mut input = Input {
        inner: BufReader::with_capacity(1 << 16, stdin.lock()),
    };
    let mut stdout = BufWriter::new(io::stdout().lock());
    let mut blobs: Vec<Blob> = Vec::new();
    while let Some(cmd) = input.line()? {
        let response = match cmd.as_str() {
            "LOAD" => {
                let (loaded, out) = cmd_load(&mut input)?;
                blobs = loaded;
                out
            }
            "PROBE" => cmd_probe(&mut input, &blobs)?,
            "SCAN" => cmd_scan(&mut input, &mut blobs)?,
            "QUIT" => break,
            "" => continue,
            other => return Err(bad(&format!("unknown command {other:?}"))),
        };
        stdout.write_all(response.as_bytes())?;
        stdout.write_all(b"\n")?;
        stdout.flush()?;
    }
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("oss-tip-audit-native: {e}");
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pattern(src: &str, flags: i64) -> Pattern {
        compile("t".into(), src, flags, vec![], HashSet::new()).unwrap()
    }

    #[test]
    fn a_rule_with_no_tokens_is_scanned_not_skipped() {
        assert!(prefilter_hit(&[], "anything"));
        assert!(prefilter_hit(&["any".to_string()], "anything"));
        assert!(!prefilter_hit(&["other".to_string()], "anything"));
    }

    #[test]
    fn dollar_becomes_python_end() {
        assert_eq!(translate("a$", 0).unwrap(), r"a(?=\n?\z)");
    }

    #[test]
    fn refuses_what_it_cannot_mirror() {
        for src in [
            r"(a)\1",
            "(?P=x)",
            "[a&&b]",
            "[a[b]]",
            "a*+",
            "(?x)a",
            r"\N{DASH}",
            r"a\B",
        ] {
            assert!(translate(src, 0).is_err(), "{src}");
        }
        assert!(translate("a", 8).is_err(), "MULTILINE must be refused");
        assert!(
            compile("t".into(), "a?", 0, vec![], HashSet::new()).is_err(),
            "empty match"
        );
    }

    #[test]
    fn literal_braces_stay_literal() {
        let re = Regex::new(&translate("a{x}b", 0).unwrap()).unwrap();
        assert!(re.is_match("a{x}b").unwrap());
        let re = Regex::new(&translate("a{,2}b", 0).unwrap()).unwrap();
        assert!(re.is_match("aab").unwrap());
    }

    #[test]
    fn word_class_is_pythons() {
        let re = Regex::new(&format!("^{}$", translate(r"\w", 0).unwrap())).unwrap();
        assert!(
            re.is_match("\u{b2}").unwrap(),
            "superscript two is alnum in Python"
        );
        assert!(!re.is_match("\u{300}").unwrap(), "a combining mark is not");
    }

    #[test]
    fn windows_count_characters_not_bytes() {
        let text = "\u{e9}\u{e9}xy\nz";
        assert_eq!(window_before(text, 4, 1), "\u{e9}");
        assert_eq!(window_after(text, 4, 10), ("xy\n", false));
        assert_eq!(window_after(text, 7, 10), ("z", true));
    }

    #[test]
    fn the_candidate_search_finds_what_the_whole_text_search_finds() {
        // Reserved domains only: the gate scans this file too.
        let text = "x@y.test a\nb (?<!q)zz@ex.example\n\nlast@x.invalid\nq.r@s.tt.test\n\u{e9}a@b.example zzz@@q.test";
        let cases = [
            (r"(?<![\w.-])[\w.]+@[a-z]+\.[a-z]{2,}\b", 2),
            (r"z+(?=@)", 0),
            (r"[a-z]+$", 0),
            (r"[a-z]+(?=\n\n)", 0),
            (r"(?<!z)z", 0),
            (r"[a-z]+\.(?=[a-z]+\.[a-z]+@)|r@s", 0),
        ];
        for (src, flags) in cases {
            let p = pattern(src, flags);
            assert!(p.fast.is_some(), "{src} should use the candidate search");
            let whole = Pattern {
                fast: None,
                ..pattern(src, flags)
            };
            let got = find_all(text, &p).unwrap();
            assert_eq!(got, find_all(text, &whole).unwrap(), "{src}");
            assert!(!got.is_empty(), "{src} found nothing, so it proved nothing");
        }
    }
}
