---
title: CRV Edge User Identity with Keycloak
description: Design for replacing the interim single local owner with Keycloak sign-in on the CRV edge, covering deployment, realm, token claims, backend and frontend changes, and tests.
author: CRV Research
ms.date: 2026-10-28
ms.topic: concept
keywords:
  - keycloak
  - oidc
  - edge
  - identity
estimated_reading_time: 10
---

## Status

Design only. The edge starts with the interim mode below; Keycloak is built later. Nothing here
changes the Azure deployment, which keeps its current identity configuration.

## Current behavior

The backend selects one identity at startup in
[bootstrap.py](../src/backend/crv_agent/runtime/bootstrap.py):

* `LocalIdentity` returns a fixed owner (`CRV_LOCAL_OWNER`) for every request. The CLI refuses to
  serve it on anything but loopback ([`__main__.py`](../src/backend/crv_agent/__main__.py)).
* `EntraIdentity` validates an RS256 bearer token against the Entra v2.0 issuer for one tenant and
  audience, requires `exp`, `nbf`, `iat`, `iss`, `aud`, `tid` and `oid`, and returns the owner
  `"{tenant}:{oid}"` ([identity.py](../src/backend/crv_agent/api/identity.py)).

The owner string scopes every thread, run and feedback row in agent memory
([persistence.py](../src/backend/crv_agent/adapters/postgres/persistence.py)): each query filters on
`owner`. The column is plain `TEXT`.

The web front end has no sign-in code. It calls the API on its own origin with `fetch`, including
the streamed `POST /api/v1/chat` and the ranged source-PDF requests, and sends no `Authorization`
header ([client.ts](../src/web/Crv-frontend/src/api/client.ts)). nginx proxies `/api/v1/` to the
backend on loopback ([nginx.conf](../src/web/Crv-frontend/nginx.conf)).

## Interim mode

Until Keycloak runs, the edge uses the AKS pattern: one shared local owner, backend on loopback,
nginx as the only listener ([compose.yaml](../infra/dgx/edge/compose.yaml)).

* Bind the web port to loopback (default) or to an interface reachable only by approved users.
* Every visitor shares one conversation history. Do not expose the edge to general LAN users.

Leave interim mode when all exit criteria hold:

* Keycloak runs on the edge with its data on the encrypted volume and a tested realm backup.
* The backend accepts only tokens from the edge realm and audience, with tests covering rejection.
* The front end signs in, refreshes tokens and sends them on every API call, including chat
  streams and source PDF requests.
* Named users exist, and administrator access is separate from user access.

## Deployment

| Aspect | Design |
|--------|--------|
| Placement | Keycloak container on dgx-niz01 in the edge Compose stack |
| Storage | Its own database (`keycloak`) in the edge PostgreSQL, owned by a dedicated login; data stays on the LUKS volume |
| Network | Same internal database network; the browser reaches it through nginx under `/auth/` |
| TLS | nginx terminates TLS for the web origin once users leave loopback; Keycloak runs with proxy headers trusted only from nginx |
| Admin console | Not proxied by nginx; reachable over SSH tunnel to loopback only |
| Internet | Not required; the backend fetches signing keys from the local issuer |

## Realm and clients

* One realm, for example `crv-edge`.
* A public client for the single-page app (`crv-web`): Authorization Code with PKCE (S256), exact
  redirect URIs on the edge origin, no implicit flow, no client secret in the browser.
* An audience for the API (`crv-api`), added to access tokens by an audience mapper or client scope,
  so the backend can require `aud = crv-api`.
* Short access tokens (5 to 10 minutes) with refresh tokens bound to the session; SSO session idle
  and maximum lifetimes set by the application owner.
* Local accounts first; LDAP or Active Directory federation later if the site requires it.
  Require multi-factor authentication for administrators, and for users if site policy requires it.

## Token claims and owner mapping

The backend needs a stable, unique, non-reassignable user key. Keycloak's `sub` is a UUID that is
stable for a user within a realm.

* Owner: `"{issuer_id}:{sub}"`, where `issuer_id` is a short stable digest of the issuer URL.
  The prefix keeps edge owners distinct from Entra owners (`"{tenant}:{oid}"`) if data ever meets.
* Required claims: `exp`, `iat`, `iss`, `aud`, `sub`; `nbf` when present.
* Never derive the owner from `preferred_username` or `email`; both can change or be reused.

Edge memory starts empty, so no existing owners need migrating.

## Backend change

Add an `OidcIdentity` beside `EntraIdentity` implementing the same `Identity` protocol:

| Setting | Meaning |
|---------|---------|
| `CRV_OIDC_ISSUER` | Exact issuer, for example `https://edge.example/auth/realms/crv-edge` |
| `CRV_OIDC_AUDIENCE` | Required audience, for example `crv-api` |
| `CRV_OIDC_OWNER_CLAIM` | Claim used for the owner key (default `sub`) |
| `CRV_OIDC_JWKS_URL` | Optional; otherwise read from the issuer's discovery document |
| `CRV_OIDC_ALGORITHMS` | Allow-list, default `["RS256"]` |

Behavior to implement:

* Validate signature, issuer, audience, expiry and not-before with a small clock-skew allowance;
  hosts run NTP.
* Fetch the JWKS from the local issuer only, cache it, and refresh on an unknown key ID with a rate
  limit, so key rotation works without restarts.
* Reject `alg: none`, symmetric algorithms and tokens whose `typ` is not an access token.
* Extend the settings validator: exactly one of local owner, Entra, or OIDC.
* Allow the backend to keep binding to loopback behind nginx, as today.

## Front end change

* Add an OIDC client library (for example `oidc-client-ts`) with Authorization Code and PKCE.
* Attach `Authorization: Bearer <access token>` in the shared `fetch` wrapper, the chat stream and
  the source PDF requests in `client.ts`.
* Refresh silently before expiry; on a 401, sign in again without losing the typed question.
* Keep tokens in memory; avoid `localStorage`.

## Threats and controls

| Threat | Control |
|--------|---------|
| Token for another client accepted | Require the exact `aud` and `iss` |
| Forged token with a weak algorithm | Algorithm allow-list; ignore the token's own key hints |
| Stale keys after rotation | JWKS cache with refresh on unknown key ID |
| Clock skew | NTP on both hosts; small skew allowance |
| Shared identity during interim mode | Loopback or restricted binding until the exit criteria hold |
| Realm loss | Encrypted, tested realm export with the database backups (P8) |

## Tests

* Unit tests with locally generated RSA keys: valid token, wrong issuer, wrong audience, expired,
  not yet valid, unknown key ID with JWKS refresh, `alg: none`, HS256, missing `sub`.
* Settings tests: one identity mode at a time.
* Integration test against a Keycloak container: sign in with a test user, call `/api/v1/` with
  the token, confirm two users see only their own threads.
* Front end tests: token attached to every request type, refresh before expiry, sign-in on 401.
