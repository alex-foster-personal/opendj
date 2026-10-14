"""The Host the webui test clients send (split out for BigRfc-1; imports nothing)."""

# request_guard's host allowlist (issue #2689) checks the literal HTTP Host
# header; httpx's TestClient default base_url sends "testserver", which
# matches nothing in the allowlist. Every app fixture (tests/webui/
# app_fixtures.py) passes hostname="test-host" to create_app(), so pinning
# base_url to that same name is the one consistent value the app will
# actually accept.
TEST_HOST_BASE_URL = "http://test-host"
