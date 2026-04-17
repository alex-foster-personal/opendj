//! search_bench -- micro-benchmark for `search_tracks_impl`.
//!
//! Invoked by `apps/launcher/scripts/latency_check.py` to confirm P95 < 50ms
//! at 5K tracks. Prints `p50: <ms>, p95: <ms>, max: <ms>` on stdout.
//!
//! Usage:
//!   HYPERK_DB_PATH=/path/to/fixture.sqlite cargo run --bin search_bench -- <query>

use std::env;
use std::process;
use std::time::Instant;

use launcher::commands::search::search_tracks_impl;
use rusqlite::Connection;

fn main() {
    let query = env::args().nth(1).unwrap_or_else(|| "dua".into());
    let db_path = env::var("HYPERK_DB_PATH").unwrap_or_else(|_| {
        "data/launcher-bootstrap.sqlite".into()
    });
    let conn = match Connection::open(&db_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("failed to open {db_path}: {e}");
            process::exit(2);
        }
    };
    let mut samples: Vec<u128> = Vec::with_capacity(100);
    for _ in 0..100 {
        let t = Instant::now();
        let _ = search_tracks_impl(&conn, &query, 20);
        samples.push(t.elapsed().as_micros());
    }
    samples.sort_unstable();
    let p50 = samples[samples.len() / 2];
    let p95 = samples[(samples.len() as f64 * 0.95) as usize];
    let max = *samples.last().unwrap();
    println!("p50: {:.2}ms, p95: {:.2}ms, max: {:.2}ms",
        p50 as f64 / 1000.0, p95 as f64 / 1000.0, max as f64 / 1000.0);
}
