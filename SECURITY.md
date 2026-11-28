# Security

Polar holds Slack tokens, including a user token that can post as you. Treat a bug here as serious.

## Report a problem

Do not open a public issue for a security bug. Use GitHub private vulnerability reporting on this repository.
If that is not available, contact the maintainer through the address on the GitHub profile.
Give the steps to reproduce. Do not include real tokens.

## Scope

- Bypass of the owner check (an action by someone who is not `POLAR_OWNER_ID`).
- Token leaks in logs, errors or cards.
- Posting without an owner click.
- Web API auth, Host check or body limit bypass.
- Web UI bypass: the Host, owner header, Origin or CSRF check, the body limit, the file root check, or HTML or
  script injection through card text.

## Web UI checks

The web UI (`polar/web/`, on when `POLAR_UI_PORT` is set) applies these checks to each request:

- It binds to 127.0.0.1 only. A reverse proxy is the only way in from another machine.
- `Host` must be `127.0.0.1:<port>`, `localhost:<port>` or a value in `POLAR_UI_HOSTS`. Else 403.
- With `POLAR_UI_OWNER` set, the header `POLAR_UI_OWNER_HEADER` (default `Tailscale-User-Login`) must equal it
  (constant-time compare). Else 403. Your proxy must remove that header from incoming requests and set it itself.
- Each POST needs an `Origin` that matches an allowed host and an `X-CSRF-Token` header. The token is an HMAC
  of a per-install secret in `data/web-ui-secret` (mode 0600). Body limit: 64 KB.
- Actions run through `webapi.perform`, with the same gates as the web API (`POLAR_WEB_ALLOW`).
- Pages send `Content-Security-Policy` with `script-src 'self'` and no inline script, `frame-ancestors 'none'`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.
- Card text is escaped. Markdown is rendered with raw HTML shown as text, images removed and only http, https and
  same-site links kept.
- `/files/<id>/<n>` serves only a file of that card whose real path is inside `data/files` or `data/uploads`.
  Files other than pictures, PDF and plain text download.

## Good practice for users

- Keep token files at mode 0600 in a 0700 folder.
- Rotate the tokens in Slack if a machine is lost.
- Leave `POLAR_WEB_ALLOW` empty unless you need it.
- Do not open `POLAR_UI_PORT` to a network. Use a reverse proxy with its own login and set `POLAR_UI_OWNER`.
