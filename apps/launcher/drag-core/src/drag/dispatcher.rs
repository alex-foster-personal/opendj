//! Drag dispatcher (Phase 18, Plan 18-01 Step 3 + Plan 18-02 Step 5).
//!
//! The dispatcher picks which `DragAdapter` to run based on which DJ apps are
//! currently running, per D5 in `18-CONTEXT.md`:
//!
//! 1. If exactly one of {djay, RB, Serato, Traktor} is running -> pick it.
//! 2. If zero are running -> return `Unsupported("no DJ app running")`.
//! 3. If multiple are running AND no vendor override was passed -> return
//!    `Unsupported("multiple DJ apps, need pick")` so the UI can show a
//!    target-picker strip.
//! 4. If multiple are running AND a vendor override IS passed -> use the
//!    override.
//!
//! The dispatcher is composed of trait objects so tests can swap in mock
//! adapters; the default `Dispatcher::default_set()` factory wires the real
//! adapters.
//
// @requirement("LAUNCH-02b", "LAUNCH-02c", "LAUNCH-02d")

use super::{
    DragAdapter, DragError, DragOutcome, HostBridge, Track, Vendor,
    djay::DjayAdapter, rekordbox::RekordboxAdapter, serato::SeratoAdapter,
    traktor::TraktorAdapter,
};

pub struct Dispatcher {
    adapters: Vec<Box<dyn DragAdapter>>,
}

impl Dispatcher {
    pub fn new(adapters: Vec<Box<dyn DragAdapter>>) -> Self {
        Self { adapters }
    }

    /// Factory with the default set of adapters (djay + RB + Serato + Traktor).
    /// Phase 17 will call this when the launcher shell starts. RB + Traktor
    /// adapters use the default config paths; override via
    /// `Dispatcher::new(...)` if you need custom sidecar paths.
    pub fn default_set() -> Self {
        Self::new(vec![
            Box::new(DjayAdapter::new()),
            Box::new(RekordboxAdapter::default()),
            Box::new(SeratoAdapter::new()),
            Box::new(TraktorAdapter::default()),
        ])
    }

    /// Determine which vendor should handle this drag. Pure function of the
    /// running-app list + the optional override. Returns the chosen `Vendor`
    /// or a `DragOutcome::Unsupported` reason.
    pub fn pick(
        &self,
        running_bundle_ids: &[String],
        override_vendor: Option<Vendor>,
    ) -> Result<Vendor, DragOutcome> {
        // Find vendors whose bundle_ids intersect with running_bundle_ids.
        let mut running_vendors: Vec<Vendor> = Vec::new();
        for adapter in &self.adapters {
            let vendor = adapter.vendor();
            let claim: &[&str] = adapter.bundle_ids();
            let hit = running_bundle_ids
                .iter()
                .any(|b| claim.iter().any(|c| c == b));
            if hit {
                running_vendors.push(vendor);
            }
        }

        if let Some(v) = override_vendor {
            // Override always wins if the override vendor is valid.
            if self.adapters.iter().any(|a| a.vendor() == v) {
                return Ok(v);
            }
            return Err(DragOutcome::Unsupported(format!(
                "unknown override vendor: {}",
                v.label()
            )));
        }

        match running_vendors.len() {
            0 => Err(DragOutcome::Unsupported("no DJ app running".into())),
            1 => Ok(running_vendors[0]),
            _ => Err(DragOutcome::Unsupported(
                "multiple DJ apps, need pick".into(),
            )),
        }
    }

    /// Dispatch a drag to the adapter chosen by `pick`.
    pub fn dispatch(
        &self,
        host: &dyn HostBridge,
        track: &Track,
        override_vendor: Option<Vendor>,
    ) -> Result<DragOutcome, DragError> {
        let running = host.running_app_bundle_ids();
        match self.pick(&running, override_vendor) {
            Ok(vendor) => {
                let adapter = self
                    .adapters
                    .iter()
                    .find(|a| a.vendor() == vendor)
                    .expect("pick returned a vendor not in the adapter set");
                adapter.start(host, track)
            }
            Err(outcome) => Ok(outcome),
        }
    }

    /// Borrow the adapter for a given vendor (used by the webview when the
    /// user clicks "No -- copy path" in the "did it work?" toast).
    pub fn adapter_for(&self, vendor: Vendor) -> Option<&dyn DragAdapter> {
        self.adapters
            .iter()
            .find(|a| a.vendor() == vendor)
            .map(|b| b.as_ref())
    }
}

#[cfg(test)]
mod dispatcher_tests {
    // @requirement("LAUNCH-02b", "LAUNCH-02c", "LAUNCH-02d")
    use super::*;
    use crate::drag::mock_host::MockHost;

    fn track() -> Track {
        Track::minimal("/tmp/song.mp3", "T", "A")
    }

    fn dispatcher() -> Dispatcher {
        Dispatcher::default_set()
    }

    #[test]
    fn single_app_autopick_serato() {
        let d = dispatcher();
        let picked = d.pick(&["com.serato.dj.pro".into()], None).unwrap();
        assert_eq!(picked, Vendor::Serato);
    }

    #[test]
    fn single_app_autopick_djay() {
        let d = dispatcher();
        let picked = d
            .pick(&["com.algoriddim.djay-pro-mac".into()], None)
            .unwrap();
        assert_eq!(picked, Vendor::Djay);
    }

    #[test]
    fn single_app_autopick_rekordbox_7() {
        let d = dispatcher();
        let picked = d
            .pick(&["com.alphatheta.rekordbox".into()], None)
            .unwrap();
        assert_eq!(picked, Vendor::Rekordbox);
    }

    #[test]
    fn single_app_autopick_rekordbox_6() {
        let d = dispatcher();
        let picked = d
            .pick(&["com.pioneerdj.rekordboxdj".into()], None)
            .unwrap();
        assert_eq!(picked, Vendor::Rekordbox);
    }

    #[test]
    fn single_app_autopick_traktor() {
        let d = dispatcher();
        let picked = d
            .pick(&["com.nativeinstruments.traktor".into()], None)
            .unwrap();
        assert_eq!(picked, Vendor::Traktor);
    }

    #[test]
    fn no_app_returns_unsupported() {
        let d = dispatcher();
        let err = d.pick(&["com.apple.finder".into()], None).unwrap_err();
        match err {
            DragOutcome::Unsupported(msg) => assert!(msg.contains("no DJ app")),
            other => panic!("expected Unsupported, got {:?}", other),
        }
    }

    #[test]
    fn multi_app_without_override_requires_pick() {
        let d = dispatcher();
        let err = d
            .pick(
                &[
                    "com.serato.dj.pro".into(),
                    "com.nativeinstruments.traktor".into(),
                ],
                None,
            )
            .unwrap_err();
        match err {
            DragOutcome::Unsupported(msg) => assert!(msg.contains("need pick")),
            other => panic!("expected Unsupported(need pick), got {:?}", other),
        }
    }

    #[test]
    fn multi_app_with_override_uses_override() {
        let d = dispatcher();
        let picked = d
            .pick(
                &[
                    "com.serato.dj.pro".into(),
                    "com.nativeinstruments.traktor".into(),
                ],
                Some(Vendor::Traktor),
            )
            .unwrap();
        assert_eq!(picked, Vendor::Traktor);
    }

    #[test]
    fn override_works_even_when_no_app_running() {
        let d = dispatcher();
        // Useful when the user manually chose a vendor in the target picker.
        let picked = d.pick(&[], Some(Vendor::Serato)).unwrap();
        assert_eq!(picked, Vendor::Serato);
    }

    #[test]
    fn dispatch_end_to_end_runs_serato_primary() {
        let host = MockHost::with_running(&["com.serato.dj.pro"]);
        let d = dispatcher();
        let outcome = d.dispatch(&host, &track(), None).unwrap();
        assert_eq!(outcome, DragOutcome::Started);
        assert_eq!(host.drag_calls.borrow().len(), 1);
    }

    #[test]
    fn dispatch_no_app_returns_unsupported_outcome() {
        let host = MockHost::new(); // no running apps
        let d = dispatcher();
        let outcome = d.dispatch(&host, &track(), None).unwrap();
        match outcome {
            DragOutcome::Unsupported(ref msg) => assert!(msg.contains("no DJ app")),
            other => panic!("expected Unsupported, got {:?}", other),
        }
        assert_eq!(host.drag_calls.borrow().len(), 0);
    }

    #[test]
    fn dispatch_multi_app_returns_need_pick_outcome() {
        let host = MockHost::with_running(&[
            "com.serato.dj.pro",
            "com.nativeinstruments.traktor",
        ]);
        let d = dispatcher();
        let outcome = d.dispatch(&host, &track(), None).unwrap();
        match outcome {
            DragOutcome::Unsupported(ref msg) => assert!(msg.contains("need pick")),
            other => panic!("expected Unsupported(need pick), got {:?}", other),
        }
    }

    #[test]
    fn all_four_vendors_routing_via_override() {
        let host = MockHost::new();
        let d = dispatcher();
        for v in &[Vendor::Djay, Vendor::Serato] {
            let outcome = d
                .dispatch(&host, &track(), Some(*v))
                .unwrap();
            assert_eq!(outcome, DragOutcome::Started, "vendor {:?}", v);
        }
        // Rekordbox + Traktor primary drag also starts (fallback only fires on
        // host error). Verify by looking at drag_calls count.
        for v in &[Vendor::Rekordbox, Vendor::Traktor] {
            let _ = d.dispatch(&host, &track(), Some(*v)).unwrap();
        }
        assert_eq!(host.drag_calls.borrow().len(), 4);
    }

    #[test]
    fn adapter_for_returns_matching_vendor() {
        let d = dispatcher();
        assert!(d.adapter_for(Vendor::Serato).is_some());
        assert!(d.adapter_for(Vendor::Rekordbox).is_some());
        assert!(d.adapter_for(Vendor::Traktor).is_some());
        assert!(d.adapter_for(Vendor::Djay).is_some());
    }
}
