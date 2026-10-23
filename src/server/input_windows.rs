//! One bounded stdin reader owns both startup EOF and later MCP input.
use std::io::{self, Read};
use std::pin::Pin;
use std::task::{Context, Poll};
use tokio::io::{AsyncRead, ReadBuf};
use tokio::sync::mpsc;

pub(crate) struct Input {
    messages: mpsc::Receiver<io::Result<Vec<u8>>>,
    bytes: Vec<u8>,
    offset: usize,
}

impl Input {
    pub(crate) fn new(on_startup_closed: impl FnOnce() + Send + 'static) -> io::Result<Self> {
        let (sender, messages) = mpsc::channel(16);
        // Windows cannot observe anonymous-pipe EOF without reading. Keep
        // this single reader for the entire server lifetime, transferring its
        // bounded queue to the async transport after initialization.
        std::thread::Builder::new()
            .name("mcp-input".into())
            .spawn(move || {
                let mut input = io::stdin().lock();
                loop {
                    let mut bytes = vec![0; 8192];
                    match input.read(&mut bytes) {
                        Ok(0) => break,
                        Ok(length) => {
                            bytes.truncate(length);
                            if sender.blocking_send(Ok(bytes)).is_err() {
                                break;
                            }
                        }
                        Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                        Err(error) => {
                            let _ = sender.blocking_send(Err(error));
                            break;
                        }
                    }
                }
                on_startup_closed();
            })?;
        Ok(Self {
            messages,
            bytes: Vec::new(),
            offset: 0,
        })
    }
}

impl AsyncRead for Input {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        output: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        if output.remaining() == 0 {
            return Poll::Ready(Ok(()));
        }
        if self.offset == self.bytes.len() {
            match self.messages.poll_recv(context) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(None) => return Poll::Ready(Ok(())),
                Poll::Ready(Some(Err(error))) => return Poll::Ready(Err(error)),
                Poll::Ready(Some(Ok(bytes))) => {
                    self.bytes = bytes;
                    self.offset = 0;
                }
            }
        }
        let count = output.remaining().min(self.bytes.len() - self.offset);
        output.put_slice(&self.bytes[self.offset..self.offset + count]);
        self.offset += count;
        Poll::Ready(Ok(()))
    }
}
