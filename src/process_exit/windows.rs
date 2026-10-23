use std::os::windows::io::{AsRawHandle, OwnedHandle};
use windows_sys::Win32::Foundation::WAIT_OBJECT_0;
use windows_sys::Win32::System::Threading::{INFINITE, WaitForMultipleObjects};

pub(super) type Cancel = crate::windows::Notify;
pub(super) fn cancellation() -> Result<(crate::windows::Event, Cancel), String> {
    crate::windows::notification().map_err(|error| error.to_string())
}

pub(super) struct Observer(OwnedHandle);

impl Observer {
    pub(super) fn new(process_id: u32) -> Result<Self, String> {
        crate::windows::process_handle(process_id)
            .map(Self)
            .map_err(|error| error.to_string())
    }

    pub(super) fn wait(self) -> Result<(), String> {
        crate::windows::wait(self.0.as_raw_handle(), None)
            .map(|_| ())
            .map_err(|error| error.to_string())
    }

    pub(super) fn wait_cancellable(self, cancelled: crate::windows::Event) -> Result<bool, String> {
        let handles = [self.0.as_raw_handle(), cancelled.as_raw_handle()];
        // Both handles stay owned until this wait settles. Exit has priority
        // when cancellation and confirmed exit are both available.
        match unsafe { WaitForMultipleObjects(2, handles.as_ptr(), 0, INFINITE) } {
            WAIT_OBJECT_0 => Ok(true),
            result if result == WAIT_OBJECT_0 + 1 => Ok(false),
            _ => Err(std::io::Error::last_os_error().to_string()),
        }
    }
}
