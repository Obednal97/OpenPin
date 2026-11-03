//! Secret and personal-data scrubbing for exported bug reports (on top of the
//! path / serial / UUID scrubbing in `mod.rs`). Everything written into a
//! report goes through `scrub_secrets` via `scrub_report_text`.
//!
//! Removes: URL query strings and fragments (and user:pass@), whole paths of
//! Notion and Google Drive/Docs URLs, `Bearer`/`Basic` credentials,
//! `key=value` / `"key": "value"` secrets, prefixed API keys (`sk-`, `sk-ant-`,
//! `ntn_`, `secret_`, `ghp_`/`github_pat_`…, `hf_`, `xox?-`), e-mail
//! addresses, and long hex (≥ 32) or base64-like (≥ 32, mixed case + digits)
//! tokens. Hand-rolled scanners: the crate has no `regex` dependency.
//! The same rules are mirrored, simplified, in app/tauri-bridge.js.

pub(super) const REDACTED: &str = "[redacted]";

/// Hosts whose URL paths name a user's documents (page / file IDs, titles).
fn is_document_host(host: &str) -> bool {
    const SUFFIXES: [&str; 6] = ["notion.so", "notion.site", "notion.com", "drive.google.com", "docs.google.com", "googleusercontent.com"];
    let h = host.trim_end_matches('.');
    SUFFIXES.iter().any(|s| h == *s || h.ends_with(&format!(".{s}")))
        || (h == "www.googleapis.com" || h == "googleapis.com")
}

fn url_end(b: &[u8], start: usize) -> usize {
    let mut j = start;
    while j < b.len() {
        let c = b[j];
        if c.is_ascii_whitespace() || matches!(c, b'"' | b'\'' | b'<' | b'>' | b'`' | b')' | b']' | b'}' | b'|' | b'\\') {
            break;
        }
        j += 1;
    }
    // Trailing sentence punctuation isn't part of the URL.
    while j > start && matches!(b[j - 1], b'.' | b',' | b';' | b':' | b'!') {
        j -= 1;
    }
    j
}

/// `http(s)://…`: drop user:pass@, query and fragment; for Notion / Google
/// Drive / Docs hosts drop the whole path.
pub(super) fn scrub_urls(s: &str) -> String {
    let b = s.as_bytes();
    let lower = s.to_ascii_lowercase();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    let mut last = 0;
    while i < b.len() {
        let scheme_len = if lower[i..].starts_with("https://") {
            8
        } else if lower[i..].starts_with("http://") {
            7
        } else {
            0
        };
        if scheme_len == 0 || (i > 0 && (b[i - 1].is_ascii_alphanumeric())) {
            i += s[i..].chars().next().map(|c| c.len_utf8()).unwrap_or(1);
            continue;
        }
        let end = url_end(b, i + scheme_len);
        let url = &s[i..end];
        let rest = &url[scheme_len..];
        let auth_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
        let authority = &rest[..auth_end];
        let host_port = authority.rsplit_once('@').map(|(_, h)| h).unwrap_or(authority);
        let host = host_port.split(':').next().unwrap_or("").to_ascii_lowercase();
        let tail = &rest[auth_end..];
        let mut cleaned = format!("{}{}", &url[..scheme_len], host_port);
        if is_document_host(&host) {
            if !tail.is_empty() && tail != "/" {
                cleaned.push('/');
                cleaned.push_str(REDACTED);
            }
        } else {
            let path_end = tail.find(['?', '#']).unwrap_or(tail.len());
            cleaned.push_str(&tail[..path_end]);
            if path_end < tail.len() {
                cleaned.push('?');
                cleaned.push_str(REDACTED);
            }
        }
        out.push_str(&s[last..i]);
        out.push_str(&cleaned);
        i = end.max(i + scheme_len);
        last = i;
    }
    out.push_str(&s[last..]);
    out
}

fn is_token_byte(c: u8) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_' | b'.' | b'~' | b'+' | b'/' | b'=')
}

/// `Bearer <token>` / `Basic <token>` (case-insensitive) → `Bearer [redacted]`.
pub(super) fn scrub_auth_schemes(s: &str) -> String {
    let b = s.as_bytes();
    let lower = s.to_ascii_lowercase();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    let mut last = 0;
    while i < b.len() {
        let word = ["bearer", "basic"].into_iter().find(|w| lower[i..].starts_with(w));
        let boundary = i == 0 || !b[i - 1].is_ascii_alphanumeric();
        if let (Some(w), true) = (word, boundary) {
            let mut j = i + w.len();
            let ws_start = j;
            while j < b.len() && (b[j] == b' ' || b[j] == b'\t') {
                j += 1;
            }
            let tok_start = j;
            while j < b.len() && is_token_byte(b[j]) {
                j += 1;
            }
            // "basic setup" isn't a credential: require a token-ish length.
            if j > ws_start && tok_start > ws_start && j - tok_start >= 8 {
                out.push_str(&s[last..tok_start]);
                out.push_str(REDACTED);
                last = j;
                i = j;
                continue;
            }
        }
        i += s[i..].chars().next().map(|c| c.len_utf8()).unwrap_or(1);
    }
    out.push_str(&s[last..]);
    out
}

/// `token=…`, `"api_key": "…"`, `password: …` and friends → value redacted.
pub(super) fn scrub_key_values(s: &str) -> String {
    const KEYS: [&str; 14] = [
        "api_key", "api-key", "apikey", "x-api-key", "access_token", "refresh_token", "id_token",
        "client_secret", "x-plaud-token", "authorization", "password", "passwd", "secret", "token",
    ];
    let b = s.as_bytes();
    let lower = s.to_ascii_lowercase();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    let mut last = 0;
    while i < b.len() {
        let boundary = i == 0 || !(b[i - 1].is_ascii_alphanumeric() || b[i - 1] == b'_' || b[i - 1] == b'-');
        let key = if boundary {
            KEYS.iter().filter(|k| lower[i..].starts_with(*k)).max_by_key(|k| k.len())
        } else {
            None
        };
        if let Some(k) = key {
            let mut j = i + k.len();
            let after_key = j;
            if j < b.len() && (b[j] == b'"' || b[j] == b'\'') {
                j += 1;
            }
            while j < b.len() && b[j] == b' ' {
                j += 1;
            }
            if j < b.len() && (b[j] == b'=' || b[j] == b':') && !(after_key < b.len() && b[after_key].is_ascii_alphanumeric()) {
                j += 1;
                while j < b.len() && b[j] == b' ' {
                    j += 1;
                }
                let quote = if j < b.len() && (b[j] == b'"' || b[j] == b'\'') { Some(b[j]) } else { None };
                if quote.is_some() {
                    j += 1;
                }
                let v_start = j;
                while j < b.len() {
                    let c = b[j];
                    let stop = match quote {
                        Some(q) => c == q || c == b'\n',
                        None => c.is_ascii_whitespace() || matches!(c, b',' | b';' | b'&' | b')' | b'}' | b']' | b'"' | b'\''),
                    };
                    if stop {
                        break;
                    }
                    j += 1;
                }
                if j > v_start && &s[v_start..j] != REDACTED {
                    out.push_str(&s[last..v_start]);
                    out.push_str(REDACTED);
                    last = j;
                    i = j;
                    continue;
                }
            }
        }
        i += s[i..].chars().next().map(|c| c.len_utf8()).unwrap_or(1);
    }
    out.push_str(&s[last..]);
    out
}

/// Provider API keys by prefix (body ≥ 16 chars) → `<prefix>[redacted]`.
pub(super) fn scrub_prefixed_keys(s: &str) -> String {
    // Longest first so `sk-ant-` wins over `sk-`.
    const PREFIXES: [&str; 15] = [
        "github_pat_", "sk-proj-", "sk-ant-", "secret_", "xoxb-", "xoxp-", "ntn_", "ghp_", "gho_",
        "ghu_", "ghs_", "ghr_", "sk-", "hf_", "pk-",
    ];
    let b = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    let mut last = 0;
    while i < b.len() {
        let boundary = i == 0 || !(b[i - 1].is_ascii_alphanumeric() || b[i - 1] == b'_' || b[i - 1] == b'-');
        if boundary {
            if let Some(p) = PREFIXES.iter().find(|p| s[i..].starts_with(*p)) {
                let mut j = i + p.len();
                while j < b.len() && (b[j].is_ascii_alphanumeric() || b[j] == b'_' || b[j] == b'-') {
                    j += 1;
                }
                if j - (i + p.len()) >= 16 {
                    out.push_str(&s[last..i]);
                    out.push_str(p);
                    out.push_str(REDACTED);
                    last = j;
                    i = j;
                    continue;
                }
            }
        }
        i += s[i..].chars().next().map(|c| c.len_utf8()).unwrap_or(1);
    }
    out.push_str(&s[last..]);
    out
}

/// `local@domain.tld` → `[email]`.
pub(super) fn scrub_emails(s: &str) -> String {
    let b = s.as_bytes();
    let is_local = |c: u8| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'%' | b'+' | b'-');
    let is_domain = |c: u8| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'-');
    let mut out = String::with_capacity(s.len());
    let mut last = 0;
    let mut i = 0;
    while i < b.len() {
        if b[i] != b'@' || i < last {
            i += 1;
            continue;
        }
        let mut l = i;
        while l > last && is_local(b[l - 1]) {
            l -= 1;
        }
        let mut r = i + 1;
        while r < b.len() && is_domain(b[r]) {
            r += 1;
        }
        let mut dom_end = r;
        while dom_end > i + 1 && matches!(b[dom_end - 1], b'.' | b'-') {
            dom_end -= 1;
        }
        let domain = &s[i + 1..dom_end];
        let tld_ok = domain
            .rsplit_once('.')
            .map(|(head, tld)| !head.is_empty() && tld.len() >= 2 && tld.bytes().all(|c| c.is_ascii_alphabetic()))
            .unwrap_or(false);
        if l < i && tld_ok {
            out.push_str(&s[last..l]);
            out.push_str("[email]");
            last = dom_end;
            i = dom_end;
            continue;
        }
        i += 1;
    }
    out.push_str(&s[last..]);
    out
}

/// Long opaque tokens: alphanumeric runs of ≥ 32 hex digits, and base64 /
/// base64url / JWT-segment runs of ≥ 32 chars mixing upper, lower and digits.
pub(super) fn scrub_long_tokens(s: &str) -> String {
    let b = s.as_bytes();
    let is_b64 = |c: u8| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'/' | b'_' | b'-' | b'=');
    let mut out = String::with_capacity(s.len());
    let mut last = 0;
    let mut i = 0;
    while i < b.len() {
        if !is_b64(b[i]) {
            i += s[i..].chars().next().map(|c| c.len_utf8()).unwrap_or(1);
            continue;
        }
        let mut j = i;
        while j < b.len() && is_b64(b[j]) {
            j += 1;
        }
        let run = &s[i..j];
        if looks_like_b64_secret(run) {
            out.push_str(&s[last..i]);
            out.push_str(REDACTED);
            last = j;
        } else {
            // Hex secrets inside the run (split on non-alphanumerics).
            let mut k = i;
            while k < j {
                if !b[k].is_ascii_alphanumeric() {
                    k += 1;
                    continue;
                }
                let mut e = k;
                while e < j && b[e].is_ascii_alphanumeric() {
                    e += 1;
                }
                if e - k >= 32 && b[k..e].iter().all(|c| c.is_ascii_hexdigit()) {
                    out.push_str(&s[last..k]);
                    out.push_str(REDACTED);
                    last = e;
                }
                k = e;
            }
        }
        i = j;
    }
    out.push_str(&s[last..]);
    out
}

fn looks_like_b64_secret(run: &str) -> bool {
    let core = run.trim_end_matches('=');
    if core.len() < 32 {
        return false;
    }
    let has = |f: fn(&u8) -> bool| core.as_bytes().iter().any(f);
    if !(has(u8::is_ascii_uppercase) && has(u8::is_ascii_lowercase) && has(u8::is_ascii_digit)) {
        return false;
    }
    // Paths like Frameworks/Python3/Versions are made of short segments;
    // random base64 rarely is.
    !core.contains('/') || core.split('/').any(|seg| seg.len() >= 24)
}

pub(super) fn scrub_secrets(s: &str) -> String {
    let t = scrub_urls(s);
    let t = scrub_auth_schemes(&t);
    let t = scrub_key_values(&t);
    let t = scrub_prefixed_keys(&t);
    let t = scrub_emails(&t);
    scrub_long_tokens(&t)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn emails() {
        assert_eq!(scrub_secrets("from ollie.b+x@example.co.uk, ok"), "from [email], ok");
        assert_eq!(scrub_secrets("<a_b@sub.mail.example.com>."), "<[email]>.");
        // Not addresses
        for keep in ["@app.route", "pkg@2.0", "a @ b", "user@localhost", "x@y.1"] {
            assert_eq!(scrub_secrets(keep), keep);
        }
    }

    #[test]
    fn auth_schemes() {
        assert_eq!(
            scrub_secrets("Authorization: Bearer eyJ0eXAi.OiJKV1Qi.LCJhbGc"),
            "Authorization: [redacted] [redacted]"
        );
        assert_eq!(scrub_auth_schemes("header bearer abcdefgh12345678"), "header bearer [redacted]");
        assert_eq!(scrub_auth_schemes("Basic dXNlcjpwYXNzd29yZA=="), "Basic [redacted]");
        assert_eq!(scrub_auth_schemes("basic setup failed"), "basic setup failed");
    }

    #[test]
    fn key_values() {
        assert_eq!(scrub_secrets("token=abc123&x=1"), "token=[redacted]&x=1");
        assert_eq!(scrub_secrets("{'api_key': 'k-1', \"password\": \"hunter 2\"}"), "{'api_key': '[redacted]', \"password\": \"[redacted]\"}");
        assert_eq!(scrub_secrets("X-Plaud-Token: zz9"), "X-Plaud-Token: [redacted]");
        assert_eq!(scrub_secrets("client_secret = shh"), "client_secret = [redacted]");
        // A key name in prose, or as part of another word, is left alone.
        for keep in ["token expired", "tokenizer=x", "no secrets here", "my_token=1x"] {
            assert_eq!(scrub_secrets(keep), keep);
        }
    }

    #[test]
    fn prefixed_api_keys() {
        let cases = [
            ("key sk-abcdefghijklmnop1234 end", "key sk-[redacted] end"),
            ("sk-proj-AbCdEfGhIjKlMnOpQrSt_uv", "sk-proj-[redacted]"),
            ("sk-ant-api03-AbCdEfGhIjKlMnOpQrSt-uvWX", "sk-ant-[redacted]"),
            ("ntn_123456789012345678901234567890abcdefghij", "ntn_[redacted]"),
            ("(secret_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefg)", "(secret_[redacted])"),
            ("ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "ghp_[redacted]"),
            ("github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz", "github_pat_[redacted]"),
            ("hf_AbCdEfGhIjKlMnOpQrStUvWxYz", "hf_[redacted]"),
            ("xoxb-1234567890-abcdefghijkl", "xoxb-[redacted]"),
        ];
        for (input, want) in cases {
            assert_eq!(scrub_prefixed_keys(input), want, "{input}");
        }
        // Too short or inside a word: kept.
        for keep in ["sk-learn", "task-abcdefghijklmnopqrstu", "hf_hub", "secret_key", "risk-assessment-for-everyone-x"] {
            assert_eq!(scrub_prefixed_keys(keep), keep);
        }
    }

    #[test]
    fn long_tokens() {
        let hex = "0123456789abcdef0123456789abcdef";
        assert_eq!(scrub_secrets(&format!("sha {hex} ok")), "sha [redacted] ok");
        assert_eq!(scrub_secrets(&format!("id={hex}")), "id=[redacted]");
        assert_eq!(scrub_secrets(&format!("page-{hex}")), "page-[redacted]");
        assert_eq!(scrub_secrets("short abcdef0123456789abcd"), "short abcdef0123456789abcd");
        let b64 = "QWxhZGRpbjpvcGVuIHNlc2FtZQ9Xy7Zk+/aBcD==";
        assert_eq!(scrub_secrets(&format!("blob {b64}.")), "blob [redacted].");
        let jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ.c2lnbmF0dXJlMTIzNDU2Nzg5MEFCQ0RFRg";
        assert!(!scrub_secrets(jwt).contains("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"));
        // Not secrets: identifiers, paths, BLE UUIDs, lowercase-only words.
        for keep in [
            "transcribe_with_speaker_diarization_and_alignment",
            "Contents/Frameworks/Python3/Versions/Current/Resources",
            "00002bb1-0000-1000-8000-00805f9b34fb",
            "3F2504E0-4F89-11D3-9A0C-0305E82C3301",
            "CoreBluetoothManagerDelegateCallbackHandler",
        ] {
            assert_eq!(scrub_secrets(keep), keep);
        }
    }

    #[test]
    fn urls() {
        assert_eq!(
            scrub_secrets("GET http://127.0.0.1:5123/audio?token=abc&id=7 failed"),
            "GET http://127.0.0.1:5123/audio?[redacted] failed"
        );
        assert_eq!(scrub_urls("see https://example.com/a/b#frag."), "see https://example.com/a/b?[redacted].");
        assert_eq!(scrub_urls("https://user:pw@example.com/x"), "https://example.com/x");
        assert_eq!(
            scrub_urls("(https://www.notion.so/myteam/Meeting-notes-0123456789abcdef0123456789abcdef)"),
            "(https://www.notion.so/[redacted])"
        );
        assert_eq!(scrub_urls("https://api.notion.com/v1/pages/abc"), "https://api.notion.com/[redacted]");
        assert_eq!(scrub_urls("'https://drive.google.com/file/d/1AbC/view?usp=x'"), "'https://drive.google.com/[redacted]'");
        assert_eq!(scrub_urls("https://docs.google.com/document/d/1x/edit"), "https://docs.google.com/[redacted]");
        assert_eq!(scrub_urls("https://www.googleapis.com/upload/drive/v3/files/abc"), "https://www.googleapis.com/[redacted]");
        assert_eq!(scrub_urls("https://xnotion.so.evil.com/p?q=1"), "https://xnotion.so.evil.com/p?[redacted]");
        assert_eq!(scrub_urls("plain https://example.com/"), "plain https://example.com/");
        assert_eq!(scrub_urls("no url here: http"), "no url here: http");
    }

    #[test]
    fn leaves_ordinary_errors_alone() {
        for keep in [
            "API error: /device/sync — Not connected",
            "Traceback (most recent call last):\n  File \"~/x.py\", line 12, in sync\nValueError: bad frame 3",
            "café — ✓ résumé",
        ] {
            assert_eq!(scrub_secrets(keep), keep);
        }
    }
}
