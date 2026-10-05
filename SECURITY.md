# Security policy

## Supported versions

Kairo has no versioned releases yet. Security fixes go to the latest revision of the default
branch only.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting:
open the repository's **Security** tab and choose **Report a vulnerability**
(https://github.com/ksgix/kairo/security/advisories/new). Do not open a public issue.

If that option is not available on the repository, open a public issue that only asks the
maintainer for a private contact channel, without any details of the vulnerability.

Please include the affected revision, how to reproduce the issue, and what impact you expect.

## Security model, in brief

Kairo is a runtime with broad authority over the host it runs on, by design. Its protections
are about who can control it, not about restricting what it does:

- **Operator socket.** The operator interface is a local Unix socket created with mode `0600`.
  There is no network listener. Whoever can open the socket is the operator.
- **Dashboard.** The optional browser dashboard listens on loopback only and accepts only its
  own `Host` names, to defend against DNS rebinding. It requires a login, every state change
  needs the session's CSRF token and a same-origin `Origin`, and a strict Content-Security-Policy
  applies. It holds no Kairo state. See [docs/dashboard.md](docs/dashboard.md).
- **Secrets.** Kairo redacts secrets before anything leaves the socket.
- **Self-deployment.** Kairo runs immutable releases built from commits. A release must pass
  preflight before it runs, and the supervisor and fallback are recovery infrastructure, not a
  sandbox. See [docs/self-maintenance.md](docs/self-maintenance.md).

In scope: bypassing the socket or dashboard access controls, leaking secrets through the
operator interface or the dashboard, and getting an unverified revision to run. Out of scope:
things the operator account can already do by design.
