# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly.

**Do not open a public issue.**

Instead, use [GitHub Security Advisories](../../security/advisories/new) to report the vulnerability privately. If that is not available, email the maintainers directly.

We will acknowledge receipt within 48 hours and aim to release a fix within 7 days for critical issues.

## Scope

This plugin fetches URLs and calls third-party search APIs. Security-relevant areas include:

- URL validation and SSRF prevention
- Proxy credential handling
- API key storage and transmission
- Content extraction from untrusted HTML
