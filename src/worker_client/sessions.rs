//! Capacity belongs to live sessions and their accepted startup work.

use std::sync::{Arc, Mutex};

#[derive(Clone)]
pub(crate) struct SessionPool(Arc<Mutex<Pool>>);

struct Pool {
    maximum: Option<usize>,
    reserved: usize,
}

#[derive(Clone)]
pub(crate) struct SessionSlot(Arc<Mutex<Slot>>);

struct Slot {
    pool: SessionPool,
    reserved: bool,
    permanent: bool,
    worker_retained: bool,
    preparation_retained: bool,
    activations: usize,
}

/// Keeps admission and preparation counted until execution owns the session.
pub(crate) struct SessionActivation(SessionSlot);

impl SessionPool {
    pub(crate) fn new(maximum: Option<usize>) -> Self {
        assert!(maximum.is_none_or(|maximum| maximum > 0));
        Self(Arc::new(Mutex::new(Pool {
            maximum,
            reserved: 0,
        })))
    }

    pub(crate) fn default_slot(&self) -> SessionSlot {
        self.reserve(true)
            .expect("the default session reserves the first slot")
    }

    pub(crate) fn try_reserve(&self) -> Result<SessionSlot, String> {
        self.reserve(false)
    }

    fn reserve(&self, permanent: bool) -> Result<SessionSlot, String> {
        self.acquire()?;
        Ok(SessionSlot(Arc::new(Mutex::new(Slot {
            pool: self.clone(),
            reserved: true,
            permanent,
            worker_retained: false,
            preparation_retained: false,
            activations: 0,
        }))))
    }

    fn acquire(&self) -> Result<(), String> {
        let mut pool = self.0.lock().expect("session capacity lock");
        if let Some(maximum) = pool.maximum
            && pool.reserved >= maximum
        {
            return Err(format!(
                "session limit reached (sessions.max = {maximum}); stop a named session before starting another"
            ));
        }
        pool.reserved += 1;
        Ok(())
    }

    fn release(&self) {
        self.0.lock().expect("session capacity lock").reserved -= 1;
    }
}

impl SessionSlot {
    pub(super) fn activate(&self) -> Result<SessionActivation, String> {
        let mut slot = self.0.lock().expect("session slot lock");
        if !slot.reserved {
            slot.pool.acquire()?;
            slot.reserved = true;
        }
        slot.activations += 1;
        Ok(SessionActivation(self.clone()))
    }

    /// A registered worker outlives request observers until retirement is confirmed.
    pub(super) fn retain_worker(&self) {
        let mut slot = self.0.lock().expect("session slot lock");
        assert!(slot.reserved);
        slot.worker_retained = true;
    }

    pub(super) fn worker_retired(&self) {
        let mut slot = self.0.lock().expect("session slot lock");
        slot.worker_retained = false;
        slot.release_if_idle();
    }

    pub(super) fn retain_preparation(&self) {
        let mut slot = self.0.lock().expect("session slot lock");
        assert!(slot.reserved);
        slot.preparation_retained = true;
    }

    pub(super) fn preparation_retired(&self) {
        let mut slot = self.0.lock().expect("session slot lock");
        slot.preparation_retained = false;
        slot.release_if_idle();
    }
}

impl Slot {
    fn release_if_idle(&mut self) {
        if self.reserved
            && !self.permanent
            && !self.worker_retained
            && !self.preparation_retained
            && self.activations == 0
        {
            self.pool.release();
            self.reserved = false;
        }
    }
}

impl Drop for Slot {
    fn drop(&mut self) {
        if self.reserved {
            self.pool.release();
        }
    }
}

impl Drop for SessionActivation {
    fn drop(&mut self) {
        let mut slot = self.0.0.lock().expect("session slot lock");
        slot.activations -= 1;
        slot.release_if_idle();
    }
}
