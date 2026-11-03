//! `plaud://login?auth_code=…` sign-in links.
//!
//! After signing in at web.plaud.ai, "Continue to desktop" opens
//! `plaud://login?auth_code=<code>&auto=0`. macOS hands the URL to this app
//! (the `plaud` scheme is registered through `plugins.deep-link` in
//! tauri.conf.json, which puts it in the bundle's CFBundleURLTypes) as an
//! Apple Event, whether the app is already running or is launched by it.
//!
//! Delivery to the page: the code is kept here until the page takes it with
//! the `take_pending_sign_in_code` command, and a `plaud-sign-in-link` event
//! (no payload) tells a loaded page to take it. tauri-bridge.js checks on
//! that event and once after the page has loaded (`has_pending_sign_in_code`),
//! waits until the backend is ready, then takes the code and hands it to the
//! `oauth-callback` listeners (renderer.js).
//! So a link that arrives before the page (launch by the link) or the backend
//! is up, or during a reload, is not lost, and `take` hands each code out once.
//!
//! The code is a credential: it is never logged, and never put in an event.

use std::sync::Mutex;

use tauri::{AppHandle, Emitter, Manager, Runtime};

/// Event telling the page that a sign-in code is waiting.
pub const SIGN_IN_LINK_EVENT: &str = "plaud-sign-in-link";

/// Upper bound for an auth code; real ones are well under 1 KB.
const MAX_CODE_LEN: usize = 4096;

#[derive(Default)]
struct SignInState {
    /// Waiting for the page to take it.
    pending: Option<String>,
    /// The last code accepted, so the same link reported twice (the plugin's
    /// startup value and its open-URL event) is only exchanged once.
    last: Option<String>,
}

static STATE: Mutex<SignInState> = Mutex::new(SignInState {
    pending: None,
    last: None,
});

/// The decoded auth code of a `plaud://login?auth_code=…` URL, or `None` for
/// any other URL (other scheme or host, no/empty/odd `auth_code`).
///
/// Decodes `%XX` only: `+` stays `+`, since the code may hold base64.
pub fn sign_in_code(url: &url::Url) -> Option<String> {
    if !url.scheme().eq_ignore_ascii_case("plaud") {
        return None;
    }
    if !url
        .host_str()
        .is_some_and(|h| h.eq_ignore_ascii_case("login"))
    {
        return None;
    }
    if !matches!(url.path(), "" | "/") {
        return None;
    }
    let raw = url
        .query()?
        .split('&')
        .find_map(|pair| pair.strip_prefix("auth_code="))?;
    let code = percent_encoding::percent_decode_str(raw)
        .decode_utf8()
        .ok()?
        .into_owned();
    let valid = !code.is_empty()
        && code.len() <= MAX_CODE_LEN
        && !code.chars().any(|c| c.is_whitespace() || c.is_control());
    valid.then_some(code)
}

/// Handle URLs macOS asked the app to open: accept the first Plaud sign-in
/// link, ignore the rest.
pub fn handle_urls<R: Runtime>(app: &AppHandle<R>, urls: &[url::Url]) {
    let Some(code) = urls.iter().find_map(sign_in_code) else {
        if !urls.is_empty() {
            log::info!("ignored a link that is not a Plaud sign-in link");
        }
        return;
    };
    {
        let mut state = STATE.lock().unwrap_or_else(|e| e.into_inner());
        if state.last.as_deref() == Some(code.as_str()) {
            return;
        }
        state.last = Some(code.clone());
        state.pending = Some(code);
    }
    log::info!("received Plaud sign-in link");

    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
    let _ = app.emit(SIGN_IN_LINK_EVENT, ());
}

/// Whether a sign-in code is waiting (without taking it).
#[tauri::command]
pub fn has_pending_sign_in_code() -> bool {
    STATE
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .pending
        .is_some()
}

/// The waiting sign-in code, if any; it is handed out once.
#[tauri::command]
pub fn take_pending_sign_in_code() -> Option<String> {
    STATE
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .pending
        .take()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn code(s: &str) -> Option<String> {
        sign_in_code(&url::Url::parse(s).unwrap())
    }

    #[test]
    fn decodes_the_auth_code() {
        assert_eq!(
            code("plaud://login?auth_code=abc123%3A%3AYXdzOmV1LWNlbnRyYWwtMQ&auto=0").as_deref(),
            Some("abc123::YXdzOmV1LWNlbnRyYWwtMQ")
        );
        assert_eq!(
            code("plaud://login?auto=0&auth_code=abc::def").as_deref(),
            Some("abc::def")
        );
        assert_eq!(code("plaud://login/?auth_code=x").as_deref(), Some("x"));
        assert_eq!(code("PLAUD://LOGIN?auth_code=x").as_deref(), Some("x"));
    }

    #[test]
    fn keeps_plus_signs() {
        assert_eq!(
            code("plaud://login?auth_code=a+b%2Bc").as_deref(),
            Some("a+b+c")
        );
    }

    #[test]
    fn ignores_other_links() {
        for s in [
            "plaud://logout?auth_code=abc",
            "plaud://login/extra?auth_code=abc",
            "plaud://login?code=abc",
            "plaud://login?auth_code=",
            "plaud://login?auth_code=a%20b",
            "plaud://login?auth_code=a%0Ab",
            "plaud://login?auth_code=%FF",
            "plaud://login",
            "plaud-v2://login?auth_code=abc",
            "https://login?auth_code=abc",
        ] {
            assert_eq!(code(s), None, "{s}");
        }
        let long = format!("plaud://login?auth_code={}", "a".repeat(MAX_CODE_LEN + 1));
        assert_eq!(code(&long), None);
    }
}
