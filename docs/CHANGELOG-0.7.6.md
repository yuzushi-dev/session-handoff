# 0.7.6 — 2026-10-22

Security release for the eMudhra intermediate CA incident.

- Restrict Node.js used by npm tooling to canary-verified release floors: `>=22.23.4 <23`, `>=24.22.1 <25`, or `>=26.12.1 <27`.
- Pin CI to Node.js `22.23.4` and npm publishing to Node.js `24.22.1`; both reject the two published eMudhra certificate-chain canaries with `CERT_REVOKED`.
- Pin `@modelcontextprotocol/client` to `2.3.1`, above the advisory's fixed floor of `2.3.0`, so the OpenAI UI build does not resolve a version affected by GHSA-6prh-2h8m-c8cw.
- Keep the distributed plugin runtime unchanged on Python and the host operating-system trust store.
