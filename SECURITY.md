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
- Web UI bypass: the login (session cookie), the Host, owner header, Origin or CSRF check, the body limit, the file root check, or HTML or
  script injection through card text.

## Web UI checks

The web UI (`polar/web/`, on when `POLAR_UI_PORT` is set) applies these checks to each request:

- It binds to 127.0.0.1 only. A reverse proxy is the only way in from another machine.
- Login: `polar ui-url` prints `http://127.0.0.1:<port>/login?token=<login token>`. `GET /login` with the right
  token sets the cookie `polar_ui` (`HttpOnly; SameSite=Strict; Path=/`, about 180 days) and answers 303 to `/`. A
  wrong or missing token: 403. Every other route except `/healthz` and `/static/` needs that cookie: a page or a file
  answers a 401 page that only says how to log in, and the API answers a 401 JSON error. The login token is
  `HMAC(secret, "polar-ui-login")` and the cookie value is `HMAC(secret, "polar-ui-session")`, where the secret is
  the web API token in `data/web-token` (mode 0600). All compares are constant-time. `polar serve` logs that the UI
  is up and names the command; it never logs the token.
- `Host` must be `127.0.0.1:<port>`, `localhost:<port>` or a value in `POLAR_UI_HOSTS`. Else 403.
- With `POLAR_UI_OWNER` set, the header `POLAR_UI_OWNER_HEADER` (default `Tailscale-User-Login`) must equal it
  (constant-time compare). Else 403. Your proxy must remove that header from incoming requests and set it itself.
- Each POST needs the session cookie, an `Origin` that matches an allowed host and an `X-CSRF-Token` header. The
  CSRF token is an HMAC of the secret and the session value. Body limit: 64 KB.
- Request framing (UI and web API): a request with `Transfer-Encoding`, with more than one `Content-Length`, with a
  `Content-Length` that is not 1 to 7 digits, or a GET or HEAD with a body gets 400 before any other check. Every
  answer with status 400 or more closes the connection, and so does any answer sent before the body was read. So a
  body is never parsed as a second request. If a file read fails after the headers went out, the connection closes. Error pages are short plain text with the same headers as a page and no Python version. A client
  that sends nothing for 30 s is disconnected.
- Actions run through `webapi.perform`, with the same gates as the web API (`POLAR_WEB_ALLOW`).
- Pages send `Content-Security-Policy` with `script-src 'self'` and no inline script, `frame-ancestors 'none'`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.
- Card text is escaped. Markdown is rendered with raw HTML shown as text, images removed and only http, https and
  same-site links kept.
- `/files/<id>/<n>` serves only a file of that card whose real path is inside `data/files` or `data/uploads`.
  Pictures and plain text show in the browser; every other file, PDF too, downloads.

## What the login protects against, and its limit

The session cookie keeps out:

- other users on the same machine: they cannot read `data/web-token`, so they cannot make the login link;
- web pages in your browser: a page on another site cannot read the cookie or the CSRF token, and `SameSite=Strict`
  keeps the cookie off its requests;
- casual scripts and tools that only find the open port.

It cannot keep out a program that runs as your own OS user. Such a program can read the secret file and the
database, so it can act as you. Polar cannot defend against that: protect your user account.

## Good practice for users

- Keep token files at mode 0600 in a 0700 folder.
- Rotate the tokens in Slack if a machine is lost.
- Leave `POLAR_WEB_ALLOW` empty unless you need it.
- Do not open `POLAR_UI_PORT` to a network. Use a reverse proxy with its own login and set `POLAR_UI_OWNER`.
- Treat the login link as a password. To log out every browser, delete `data/web-token` and restart Polar (the web
  API token changes too).
