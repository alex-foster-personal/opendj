#[cfg(test)]
mod tests {
    use std::path::Path;

    const OPENER_JSON: &str = "../../apps/desktop/src-tauri/capabilities/opener.json";
    const FOUR_DIGIT: &str = "http://opendj.localhost:[0-9][0-9][0-9][0-9]/*";
    const FIVE_DIGIT: &str = "http://opendj.localhost:[0-9][0-9][0-9][0-9][0-9]/*";
    const GOOGLE_CONSENT: &str = "https://accounts.google.com/o/oauth2/v2/auth[?]*";
    const BLACKHOLE_INSTALLER: &str = "https://existential.audio/blackhole/";

    fn load_allow_urls() -> Vec<String> {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join(OPENER_JSON);
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|err| panic!("failed to read {}: {err}", path.display()));
        let value: serde_json::Value =
            serde_json::from_str(&text).unwrap_or_else(|err| panic!("opener.json is not JSON: {err}"));
        let mut urls = Vec::new();
        for permission in value["permissions"]
            .as_array()
            .expect("opener.json permissions must be an array")
        {
            if permission["identifier"].as_str() != Some("opener:allow-open-url") {
                continue;
            }
            for entry in permission["allow"]
                .as_array()
                .expect("opener:allow-open-url allow must be an array")
            {
                urls.push(
                    entry["url"]
                        .as_str()
                        .expect("allow entry url must be a string")
                        .to_owned(),
                );
            }
        }
        urls
    }

    fn url_is_allowed(patterns: &[String], url: &str) -> bool {
        patterns.iter().any(|pattern| {
            // same calls as tauri-plugin-opener 2.5.5 Entry deserialize / matches_url; Scope::new is pub(crate).
            glob::Pattern::new(pattern).unwrap().matches(url)
        })
    }

    #[test]
    fn opener_json_scope_matches_via_plugin_glob() {
        let patterns = load_allow_urls();
        assert!(
            patterns.iter().any(|pattern| pattern == FOUR_DIGIT),
            "loaded allow list missing {FOUR_DIGIT}: {patterns:?}"
        );
        assert!(
            patterns.iter().any(|pattern| pattern == FIVE_DIGIT),
            "loaded allow list missing {FIVE_DIGIT}: {patterns:?}"
        );
        assert!(
            patterns.iter().any(|pattern| pattern == GOOGLE_CONSENT),
            "loaded allow list missing {GOOGLE_CONSENT}: {patterns:?}"
        );
        assert!(
            patterns
                .iter()
                .any(|pattern| pattern == BLACKHOLE_INSTALLER),
            "loaded allow list missing {BLACKHOLE_INSTALLER}: {patterns:?}"
        );

        assert!(url_is_allowed(&patterns, "http://opendj.localhost:7700/"));
        assert!(url_is_allowed(&patterns, "http://opendj.localhost:54042/"));
        assert!(!url_is_allowed(
            &patterns,
            "http://opendj.localhost:7700@attacker.example/"
        ));
        assert!(!url_is_allowed(
            &patterns,
            "http://opendj.localhost.attacker.example:7700/"
        ));
        assert!(!url_is_allowed(&patterns, "https://accounts.example/"));
        assert!(url_is_allowed(
            &patterns,
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=x&state=y"
        ));
        assert!(url_is_allowed(
            &patterns,
            "https://existential.audio/blackhole/"
        ));
        assert!(!url_is_allowed(
            &patterns,
            "https://accounts.google.com/o/oauth2/v2/auth@attacker.example/"
        ));
        assert!(!url_is_allowed(
            &patterns,
            "https://accounts.google.com.attacker.example/o/oauth2/v2/auth?x=1"
        ));
        assert!(!url_is_allowed(
            &patterns,
            "https://existential.audio.attacker.example/blackhole/"
        ));
        assert!(!url_is_allowed(
            &patterns,
            "https://existential.audio/blackhole/@attacker.example/"
        ));
        assert!(!url_is_allowed(
            &patterns,
            "https://notexistential.audio/blackhole/"
        ));
    }

    // Parity matrix row 35: hosted billing opens Polar checkout and the portal through openUrl.
    #[test]
    fn polar_billing_pages_are_allowed_and_lookalikes_are_not() {
        let patterns = load_allow_urls();
        for url in [
            "https://buy.polar.sh/polar_cl_example",
            "https://checkout.polar.sh/bundle",
            "https://polar.sh/opendj/portal?customer_session_token=x",
            "https://sandbox.polar.sh/checkout/polar_c_example",
        ] {
            assert!(url_is_allowed(&patterns, url), "billing url refused: {url}");
        }
        let at = "@";
        for url in [
            "http://buy.polar.sh/c/1".to_owned(),
            "https://polar.sh.attacker.example/c/1".to_owned(),
            "https://notpolar.sh/c/1".to_owned(),
            "https://attacker.example/x.polar.sh/".to_owned(),
            format!("https://polar.sh{at}attacker.example/"),
            "https://buy.polar.sh:8443/c/1".to_owned(),
            "https://example.com/".to_owned(),
        ] {
            assert!(!url_is_allowed(&patterns, &url), "non-billing url allowed: {url}");
        }
    }
}
