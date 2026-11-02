//! Sleep / wake detection.
//!
//! Primary source: `NSWorkspaceDidWakeNotification` and
//! `NSWorkspaceWillSleepNotification`, observed on the main queue. Fallback: a
//! background thread that ticks every 5 s and treats a wall-clock gap of more
//! than 30 s as a wake (confirmed against the kernel's asleep-time counter when
//! it is readable, so App Nap timer throttling or a manual clock change can't
//! fake one).
//!
//! The old detector compared `std::time::Instant`s. On macOS that clock is
//! CLOCK_UPTIME_RAW, which stops while the machine sleeps, so a real sleep never
//! showed up as a gap and `system-wake` never fired.
//!
//! Both sources go through `emit_wake`, which de-duplicates so one wake sends
//! one `system-wake` event to the frontend.

use std::sync::Mutex;
use std::time::{Duration, SystemTime};

use tauri::{AppHandle, Emitter};

/// Frontend event sent once per wake (renderer.js: `onSystemWake`).
pub const WAKE_EVENT: &str = "system-wake";
/// Frontend event sent just before the machine sleeps. Nothing listens yet.
pub const WILL_SLEEP_EVENT: &str = "system-will-sleep";

const FALLBACK_TICK: Duration = Duration::from_secs(5);
/// Wall-clock gap between two fallback ticks that counts as a sleep.
const FALLBACK_GAP: Duration = Duration::from_secs(30);
/// When the asleep-time counter is readable, the machine must also have spent
/// at least this long asleep during the gap.
const FALLBACK_MIN_ASLEEP: Duration = Duration::from_secs(10);
/// A notification arriving this soon after another reported wake is the same wake.
const NOTIFICATION_DEDUP: Duration = Duration::from_secs(20);

/// Wall-clock time of the last `system-wake` we emitted.
static LAST_WAKE: Mutex<Option<SystemTime>> = Mutex::new(None);

/// Whether a wake detected now should be reported, given the last reported one.
///
/// * `gap_started = Some(t)` (fallback): the machine was asleep at some point
///   after `t`. It's a duplicate if a wake was already reported at or after `t`.
/// * `gap_started = None` (notification): a duplicate if a wake was reported in
///   the last `NOTIFICATION_DEDUP`.
fn is_new_wake(last: Option<SystemTime>, now: SystemTime, gap_started: Option<SystemTime>) -> bool {
    let Some(prev) = last else { return true };
    match gap_started {
        Some(start) => prev < start,
        None => now
            .duration_since(prev)
            .map(|since| since >= NOTIFICATION_DEDUP)
            // Clock went backwards since the last report: treat as new.
            .unwrap_or(true),
    }
}

/// Emit `system-wake` unless this wake was already reported.
pub fn emit_wake(app: &AppHandle, source: &str, gap_started: Option<SystemTime>) {
    let now = SystemTime::now();
    {
        let mut last = LAST_WAKE.lock().unwrap_or_else(|e| e.into_inner());
        if !is_new_wake(*last, now, gap_started) {
            log::info!("[PowerMonitor] Wake from {source} already reported, skipping");
            return;
        }
        *last = Some(now);
    }
    log::info!("[PowerMonitor] System wake detected ({source})");
    let _ = app.emit(WAKE_EVENT, ());
}

/// Nanoseconds this machine has spent asleep since boot: CLOCK_MONOTONIC_RAW
/// keeps counting during sleep, CLOCK_UPTIME_RAW doesn't, and both use the
/// same raw timebase.
#[cfg(target_os = "macos")]
fn asleep_nanos() -> Option<u64> {
    fn read(clock: libc::clockid_t) -> Option<u64> {
        let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
        // SAFETY: clock_gettime only writes to the timespec we pass.
        if unsafe { libc::clock_gettime(clock, &mut ts) } != 0 {
            return None;
        }
        Some(ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64)
    }
    // Read uptime first so the difference can't come out negative.
    let uptime = read(libc::CLOCK_UPTIME_RAW)?;
    let continuous = read(libc::CLOCK_MONOTONIC_RAW)?;
    Some(continuous.saturating_sub(uptime))
}

#[cfg(not(target_os = "macos"))]
fn asleep_nanos() -> Option<u64> {
    None
}

/// Start the 5 s wall-clock fallback detector.
pub fn spawn_fallback_detector(app: AppHandle) {
    let spawned = std::thread::Builder::new()
        .name("wake-fallback".into())
        .spawn(move || {
            let mut last_wall = SystemTime::now();
            let mut last_asleep = asleep_nanos();
            loop {
                std::thread::sleep(FALLBACK_TICK);
                let now = SystemTime::now();
                let asleep = asleep_nanos();
                let gap = now.duration_since(last_wall).unwrap_or(Duration::ZERO);
                if gap > FALLBACK_GAP {
                    let slept_enough = match (last_asleep, asleep) {
                        (Some(before), Some(after)) => {
                            Duration::from_nanos(after.saturating_sub(before)) >= FALLBACK_MIN_ASLEEP
                        }
                        // Counter unreadable: trust the wall-clock gap alone.
                        _ => true,
                    };
                    if slept_enough {
                        emit_wake(&app, &format!("wall-clock gap {}s", gap.as_secs()), Some(last_wall));
                    }
                }
                last_wall = now;
                last_asleep = asleep;
            }
        });
    if let Err(e) = spawned {
        log::error!("[PowerMonitor] Couldn't start the wake fallback thread: {e}");
    }
}

/// Observe NSWorkspace sleep/wake notifications. Must run on the main thread
/// (call it from `setup`). The observers live for the rest of the process.
#[cfg(target_os = "macos")]
pub fn install_workspace_observers(app: &AppHandle) {
    use std::ptr::NonNull;

    use block2::RcBlock;
    use objc2_app_kit::{NSWorkspace, NSWorkspaceDidWakeNotification, NSWorkspaceWillSleepNotification};
    use objc2_foundation::{NSNotification, NSOperationQueue};

    let center = NSWorkspace::sharedWorkspace().notificationCenter();
    let main_queue = NSOperationQueue::mainQueue();

    let wake_app = app.clone();
    let on_wake = RcBlock::new(move |_note: NonNull<NSNotification>| {
        emit_wake(&wake_app, "NSWorkspaceDidWakeNotification", None);
    });
    let sleep_app = app.clone();
    let on_will_sleep = RcBlock::new(move |_note: NonNull<NSNotification>| {
        log::info!("[PowerMonitor] System going to sleep");
        let _ = sleep_app.emit(WILL_SLEEP_EVENT, ());
    });

    // SAFETY: the notification names are AppKit's own constants; the blocks
    // only capture an AppHandle (Send + Sync) and run on the main queue.
    unsafe {
        let wake_token = center.addObserverForName_object_queue_usingBlock(
            Some(NSWorkspaceDidWakeNotification),
            None,
            Some(&main_queue),
            &on_wake,
        );
        let sleep_token = center.addObserverForName_object_queue_usingBlock(
            Some(NSWorkspaceWillSleepNotification),
            None,
            Some(&main_queue),
            &on_will_sleep,
        );
        // The observers must outlive setup(): keep them for the process lifetime.
        std::mem::forget(wake_token);
        std::mem::forget(sleep_token);
    }
    log::info!("[PowerMonitor] Observing NSWorkspace sleep/wake notifications");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(secs: u64) -> SystemTime {
        SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000 + secs)
    }

    #[test]
    fn first_wake_is_always_new() {
        assert!(is_new_wake(None, t(0), None));
        assert!(is_new_wake(None, t(0), Some(t(0))));
    }

    #[test]
    fn fallback_after_notification_for_same_sleep_is_duplicate() {
        // Ticked at 100, slept, notification reported the wake at 500, fallback ticks at 503.
        assert!(!is_new_wake(Some(t(500)), t(503), Some(t(100))));
    }

    #[test]
    fn notification_after_fallback_for_same_sleep_is_duplicate() {
        assert!(!is_new_wake(Some(t(500)), t(501), None));
    }

    #[test]
    fn later_sleep_is_reported_again() {
        // Previous wake at 500; new gap started at 900.
        assert!(is_new_wake(Some(t(500)), t(1300), Some(t(900))));
        // Notification well past the dedup window.
        assert!(is_new_wake(Some(t(500)), t(600), None));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn asleep_counter_is_readable() {
        assert!(asleep_nanos().is_some());
    }
}
