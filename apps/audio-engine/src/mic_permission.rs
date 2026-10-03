//! The microphone permission macOS gives this app (read with feature `device`).
//!
//! Until the DJ answers the first-run prompt, macOS hands an input stream
//! silence, with no error, so a recording started then looks live and holds
//! nothing (Silver lost 42 s of a set that way). `record` reads this before
//! it writes anything: it waits while the prompt is up, and refuses when
//! access is off. macOS attributes the child process to the app, so this is
//! the app's own permission. Other systems have no such gate.

/// What the system says about recording audio input.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Permission {
    /// Allowed (always, off macOS).
    Granted,
    /// Not asked yet: the prompt shows when an input opens.
    Undetermined,
    /// Turned off by the user, or restricted by policy.
    Denied,
}

/// The current permission.
#[cfg(all(target_os = "macos", feature = "device"))]
pub fn current() -> Permission {
    use objc2::msg_send;
    use objc2::runtime::AnyClass;
    use objc2_foundation::NSString;

    #[link(name = "AVFoundation", kind = "framework")]
    extern "C" {
        static AVMediaTypeAudio: &'static NSString;
    }

    let Some(class) = AnyClass::get(c"AVCaptureDevice") else {
        // AVFoundation is linked above, so this means a broken system;
        // recording silence would hide it, so say no.
        return Permission::Denied;
    };
    // AVAuthorizationStatus: 0 not determined, 1 restricted, 2 denied, 3 authorized.
    let status: isize = unsafe { msg_send![class, authorizationStatusForMediaType: AVMediaTypeAudio] };
    from_status(status)
}

#[cfg(not(all(target_os = "macos", feature = "device")))]
pub fn current() -> Permission {
    Permission::Granted
}

/// AVAuthorizationStatus to a permission; an unknown value is not a grant.
pub fn from_status(status: isize) -> Permission {
    match status {
        3 => Permission::Granted,
        0 => Permission::Undetermined,
        _ => Permission::Denied,
    }
}

/// What the DJ reads when access is off.
pub const DENIED_MESSAGE: &str = "microphone access for Open DJ is off; turn it on in System Settings > Privacy & Security > Microphone, then press REC again";

/// How a wait for the permission ended.
#[derive(Debug, PartialEq, Eq)]
pub enum Waited {
    Granted,
    /// Stop was asked for while the prompt was still up.
    Stopped,
}

/// Wait while the permission is undetermined: `tick` runs between readings
/// (discarding the silence the input delivers meanwhile, and sleeping) and
/// returns true when stop is asked for. A refusal is an error naming where
/// to turn access on.
pub fn wait_for_grant(
    mut read: impl FnMut() -> Permission,
    mut tick: impl FnMut() -> Result<bool, String>,
) -> Result<Waited, String> {
    loop {
        match read() {
            Permission::Granted => return Ok(Waited::Granted),
            Permission::Denied => return Err(DENIED_MESSAGE.to_string()),
            Permission::Undetermined => {
                if tick()? {
                    return Ok(Waited::Stopped);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_authorized_is_a_grant() {
        assert_eq!(from_status(3), Permission::Granted);
        assert_eq!(from_status(0), Permission::Undetermined);
        for s in [1, 2, 4, -1] {
            assert_eq!(from_status(s), Permission::Denied, "{s}");
        }
    }

    #[test]
    fn the_wait_ends_on_an_answer_or_a_stop() {
        let mut answers = vec![Permission::Granted, Permission::Undetermined, Permission::Undetermined];
        let mut ticks = 0;
        let r = wait_for_grant(|| answers.pop().unwrap(), || {
            ticks += 1;
            Ok(false)
        });
        assert_eq!((r, ticks), (Ok(Waited::Granted), 2));

        let mut answers = vec![Permission::Denied, Permission::Undetermined];
        let err = wait_for_grant(|| answers.pop().unwrap(), || Ok(false)).unwrap_err();
        assert!(err.contains("Privacy & Security > Microphone"), "{err}");

        assert_eq!(wait_for_grant(|| Permission::Undetermined, || Ok(true)), Ok(Waited::Stopped));
        // A tick that fails (the input went away) ends the wait with its error.
        assert_eq!(wait_for_grant(|| Permission::Undetermined, || Err("gone".into())), Err("gone".into()));
        // Control: granted from the start never ticks.
        assert_eq!(wait_for_grant(|| Permission::Granted, || panic!("no tick")), Ok(Waited::Granted));
    }
}
