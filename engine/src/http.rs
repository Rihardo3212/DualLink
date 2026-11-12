//! One place that makes HTTP clients: Windows' own TLS (no bundled certificates), never a system proxy.

use std::sync::Arc;
use std::time::Duration;

pub fn agent(timeout: Duration) -> ureq::Agent {
    let mut b = ureq::AgentBuilder::new().timeout_connect(timeout.min(Duration::from_secs(10))).timeout(timeout).user_agent("DualLink");
    if let Ok(tls) = native_tls::TlsConnector::new() {
        b = b.tls_connector(Arc::new(tls));
    }
    b.build()
}
