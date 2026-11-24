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

## Good practice for users

- Keep token files at mode 0600 in a 0700 folder.
- Rotate the tokens in Slack if a machine is lost.
- Leave `POLAR_WEB_ALLOW` empty unless you need it.
