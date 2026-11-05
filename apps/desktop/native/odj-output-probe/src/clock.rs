//! UTC wall-clock stamps without a date-time dependency.

use std::time::{SystemTime, UNIX_EPOCH};

/// UTC RFC3339 with millisecond precision and a `+00:00` suffix.
pub fn utc_timestamp_iso() -> String {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch");
    let secs = duration.as_secs();
    let millis = duration.subsec_millis();
    let (year, month, day, hour, minute, second) = unix_secs_to_utc(secs);
    format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millis:03}+00:00"
    )
}

fn unix_secs_to_utc(secs: u64) -> (u32, u32, u32, u32, u32, u32) {
    let days = secs / 86_400;
    let time = secs % 86_400;
    let hour = (time / 3_600) as u32;
    let minute = ((time % 3_600) / 60) as u32;
    let second = (time % 60) as u32;
    let (year, month, day) = civil_from_days(days as i64);
    (year, month, day, hour, minute, second)
}

fn civil_from_days(days: i64) -> (u32, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    let year = y + if month <= 2 { 1 } else { 0 };
    (year as u32, month as u32, day as u32)
}

#[cfg(test)]
mod tests {
    use super::utc_timestamp_iso;

    #[test]
    fn utc_timestamp_ends_with_plus_zero_offset() {
        let stamp = utc_timestamp_iso();
        assert!(stamp.ends_with("+00:00"), "{stamp}");
        assert!(stamp.contains('T'), "{stamp}");
    }
}
