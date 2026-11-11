//! Loopback smartlist reads against the running packaged engine.

use serde::{Deserialize, Serialize};

use super::engine::{loopback_get, EngineOrigin};

const SMARTLISTS_PATH: &str = "/api/v1/smartlists";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SmartlistSummaryOut {
    pub id: String,
    pub name: String,
    pub rule_summary: String,
    pub order_by: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SmartlistTracksOut {
    pub smartlist_id: String,
    pub name: String,
    pub rule_summary: String,
    pub order_by: String,
    pub items: Vec<String>,
}

fn smartlists_error(status: u16, body: &str) -> String {
    if status == 503 {
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(body) {
            if let Some(code) = value
                .get("detail")
                .and_then(|d| d.get("code"))
                .and_then(|c| c.as_str())
            {
                let message = value
                    .get("detail")
                    .and_then(|d| d.get("message"))
                    .and_then(|m| m.as_str())
                    .unwrap_or("smartlists unavailable");
                return format!("{code}: {message}");
            }
        }
    }
    format!("GET {SMARTLISTS_PATH} returned status {status}: {body}")
}

pub fn list_smartlists_impl(origin: &EngineOrigin) -> Result<Vec<SmartlistSummaryOut>, String> {
    let (status, body) = loopback_get(&origin.host, origin.port, SMARTLISTS_PATH)?;
    if status != 200 {
        return Err(smartlists_error(status, &body));
    }
    let rows: Vec<serde_json::Value> =
        serde_json::from_str(&body).map_err(|e| format!("smartlists list is not JSON: {e}"))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let id = row
            .get("id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "smartlists list row missing id".to_string())?
            .to_string();
        let name = row
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let rule_summary = row
            .get("rule_summary")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let order_by = row
            .get("order_by")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        out.push(SmartlistSummaryOut {
            id,
            name,
            rule_summary,
            order_by,
        });
    }
    Ok(out)
}

pub fn smartlist_tracks_impl(
    origin: &EngineOrigin,
    smartlist_id: &str,
) -> Result<SmartlistTracksOut, String> {
    let path = format!("{SMARTLISTS_PATH}/{smartlist_id}/tracks");
    let (status, body) = loopback_get(&origin.host, origin.port, &path)?;
    if status != 200 {
        return Err(smartlists_error(status, &body));
    }
    let doc: serde_json::Value =
        serde_json::from_str(&body).map_err(|e| format!("smartlist tracks is not JSON: {e}"))?;
    Ok(SmartlistTracksOut {
        smartlist_id: doc
            .get("smartlist_id")
            .and_then(|v| v.as_str())
            .unwrap_or(smartlist_id)
            .to_string(),
        name: doc
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        rule_summary: doc
            .get("rule_summary")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        order_by: doc
            .get("order_by")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        items: doc
            .get("items")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default(),
    })
}

#[tauri::command]
pub fn list_engine_smartlists() -> Result<Vec<SmartlistSummaryOut>, String> {
    let origin = super::engine::resolve_engine_origin()?;
    list_smartlists_impl(&origin)
}

#[tauri::command]
pub fn fetch_smartlist_tracks(smartlist_id: String) -> Result<SmartlistTracksOut, String> {
    let origin = super::engine::resolve_engine_origin()?;
    smartlist_tracks_impl(&origin, &smartlist_id)
}
