# Claven — CASA Tier 2 readiness review

Scope: this repo at commit 25932b1, reviewed 2026-11-02. Read-only: nothing was changed, deployed, or run against prod. `npm audit` was run against `web/` (read-only). `pip-audit` is not installed in `.venv`, so the Python CVE findings below come from known advisories for the installed versions and still need a `pip-audit` run to confirm. Line numbers refer to files at HEAD.

## 1. Summary verdict

**Not ready yet.** The core design is sound: tokens are Fernet-encrypted with a key kept in Secret Manager, every `/api/*` route takes the user from the session, SQL is fully parameterised, Gmail access is metadata-only, OAuth uses state and a fixed redirect URI, and no secrets are committed. Six gaps are likely to fail a CASA Tier 2 scan or questionnaire: no security headers (HSTS/CSP/XFO/nosniff), sessions that never expire and can't be revoked, CSRF exposure from `SameSite=None` cookies, a Pub/Sub webhook that accepts any Google service account, no in-app data deletion or Google token revocation, and known critical/high CVEs in dependencies (Next.js critical). All six are small, contained changes.

**Counts (64 checklist rows): PASS 30 · GAP 27 · UNKNOWN 7.** The 27 GAP rows reduce to 23 distinct findings: 6 must-fix (covering 9 rows), 13 should-fix, 4 nice-to-have.

## 2. Must-fix (likely to fail CASA Tier 2)

**M1. No security headers on the API or the frontend (ASVS V14.4).**
Evidence: `claven/server.py:154-169` adds only CORS middleware. `web/next.config.ts:3` is `{}` with no `headers()`, and there is no `web/middleware.ts`/`proxy.ts`. Scanners (ZAP / Fluid Attacks) will flag missing HSTS, CSP, X-Frame-Options/frame-ancestors, X-Content-Type-Options and Referrer-Policy.
Fix: in `claven/server.py`, add a small `@app.middleware("http")` that sets `Strict-Transport-Security: max-age=31536000; includeSubDomains`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store` on `/api/*` and `/oauth/*`. In `web/next.config.ts`, add `async headers()` returning the same HSTS/nosniff/XFO/Referrer-Policy/Permissions-Policy plus a CSP that allows `connect-src 'self' https://api.claven.app` and `frame-ancestors 'none'` (Next's inline scripts need nonces or `'unsafe-inline'` for `script-src` at first). Add a test that checks the headers on both apps.

**M2. Sessions never expire and logout doesn't revoke them (ASVS 3.3.1, 3.3.2).**
Evidence: `claven/routes/auth.py:60-71`: `_issue_session` signs `{"user_id","email"}` with no `exp`/`iat` (the docstring says "No expiry is set"). `auth.py:248` sets the cookie with no `max_age`. `auth.py:252-257`: `/api/logout` only deletes the cookie, so a copied token stays valid forever. The only way to revoke one is rotating `SESSION_SECRET`, which logs out everyone.
Fix: in `_issue_session`, add `iat`, `exp` (e.g. 7 days) and a `sv` (session version) claim. Add `users.session_version INT NOT NULL DEFAULT 0` via a new alembic migration. In `_get_session`, compare `sv` to the DB value (one indexed lookup; `/api/me` already loads the user). `/api/logout` and account deletion increment `session_version`. Set `max_age` on the cookie to match `exp`, and pass `options={"require": ["exp"]}` to `pyjwt.decode`.

**M3. CSRF exposure on state-changing endpoints (ASVS 4.2.2 / 3.4.3).**
Evidence: the session cookie is `samesite="none"` (`claven/routes/auth.py:248`). POST endpoints take no body and have no CSRF token or Origin check: `/api/connect`, `/api/disconnect` (`claven/routes/api.py:508-551`) and `/api/actions/archive-unknown|reset-sent-scan|cancel` (`claven/routes/actions.py:17-84`). `/api/settings/*` uses `await request.json()` (`api.py:558,585`), which doesn't check Content-Type, so a cross-site `text/plain` form post works too. CORS (`server.py:163-169`) stops cross-site reads, not cross-site sends. Impact: any website can make a signed-in user's Gmail be archived (archive-unknown) or disconnected.
Fix: `claven.app` and `api.claven.app` are the same site, so change the cookie to `samesite="lax"` in `auth.py:248`. Also add a dependency in `claven/routes/auth.py` that rejects non-GET requests whose `Origin` header isn't in `_allowed_origins()` (or that lack a custom header such as `X-Requested-With`, which `web/app/dashboard/page.tsx` would send). Apply it to every POST in `api.py` and `actions.py`, and call `_get_session` only after it.

**M4. The Pub/Sub webhook accepts tokens from any Google service account (ASVS 4.1 / 13.x).**
Evidence: `claven/routes/auth.py:30-57`: after OIDC verification, the only identity check is `email.endswith(".gserviceaccount.com")` (`auth.py:55`). Anyone can create a Pub/Sub push subscription in their own GCP project with audience `https://api.claven.app/webhook/gmail` (the value in `deploy.yml:96`). Google then signs a valid token for the attacker's service account, which passes this check. The attacker controls the payload (`emailAddress`, `historyId`), and `claven/routes/internal.py:301,313` writes `notification_history_id` straight to the DB. A very large `historyId` makes Claven skip a user's mail; repeated posts trigger Gmail API work for any user (abuse of their quota). Also, when `PUBSUB_AUDIENCE` is unset, the audience check is skipped entirely (`auth.py:42`).
Fix: add a `PUBSUB_PUSH_SERVICE_ACCOUNT` env var (the service account configured on Claven's push subscription) and require `id_info["email"] == that` and `id_info.get("email_verified")`. Fail closed (500 or 401) if `PUBSUB_AUDIENCE` or the service-account var is unset. Defence in depth: in `internal.py`, take `historyId` from `get_profile()` (already used in `/internal/poll`, `internal.py:104-105`) instead of trusting the notification value.

**M5. No user-initiated data deletion, and OAuth tokens are never revoked with Google (ASVS 8.3.2; Google API Services User Data Policy).**
Evidence: no delete-account route exists in `claven/routes/`. `db.delete_credentials` (`claven/core/db.py:125-129`) is never called from app code. `/api/disconnect` deliberately keeps tokens (`claven/routes/api.py:536-551`). No code calls `https://oauth2.googleapis.com/revoke`. The privacy policy relies on emailing support with up to 30 days to act (`web/app/privacy/content.ts:80-82`). Assessors and Google reviewers routinely ask how users delete their data and whether tokens are revoked. The schema already cascades (`alembic/versions/0001_initial.py:40,59,76`; `0011_add_event_log.py:17`), so `DELETE FROM users` removes everything.
Fix: add `POST /api/account/delete` in `claven/routes/api.py`, with the logic in `claven/core/` (e.g. `claven/core/account.py::delete_account`). It should: cancel running jobs, call `stop_watch`, POST the refresh token to `https://oauth2.googleapis.com/revoke` (best effort), run `DELETE FROM users WHERE id=%s` via a new `db.delete_user`, bump `session_version`/clear the cookie, and log an `account_deleted` event (user_id only). Add a "Delete my account and data" button with confirmation on the dashboard (the `alert-dialog` component already exists). Update `privacy/content.ts` to describe immediate in-app deletion. Write tests first, as CLAUDE.md requires.

**M6. Known critical/high vulnerabilities in dependencies (ASVS 14.2.1; CASA SCA scan).**
Evidence: `npm audit --omit=dev` in `web/` reports 37 vulnerabilities (2 critical, 20 high). The criticals are `next` 16.2.4 (vulnerable range ≤16.4.0, including a middleware/proxy bypass and an RSC DoS) and `proxy-addr`. The highs include `@opennextjs/cloudflare`, `undici`, `ws`, `postcss`, `sharp` and `zod`. `wrangler` and `shadcn` are listed under `dependencies` (`web/package.json:16-31`) instead of `devDependencies`, which pulls many advisories into the production tree. On the Python side, the installed `starlette` 0.38.6 (from `fastapi==0.115.0`) and `cryptography==42.0.8` (`requirements.txt`) have published CVEs: starlette CVE-2024-47874 and CVE-2025-54121 (multipart DoS), and cryptography CVE-2024-12797 (bundled OpenSSL). Neither path is exercised by Claven, but SCA tools will still flag them. Confirm with `pip-audit`.
Fix: in `web/package.json`, bump `next` (and `eslint-config-next`) to the current patched release and `@opennextjs/cloudflare` to the latest, move `wrangler` and `shadcn` to `devDependencies`, run `npm audit fix`, and get `npm audit --omit=dev` to 0 high/critical. In `requirements.txt`, bump `fastapi` to a release that pulls starlette ≥0.49.1, and `cryptography` to current (≥44.0.1). Add `pip-audit` and `npm audit --omit=dev --audit-level=high` steps to `.github/workflows/ci.yml`, plus `.github/dependabot.yml`.

## 3. Full checklist

| # | Area | Requirement | Status | Evidence |
|---|------|-------------|--------|----------|
| 1 | Session | Session token signed; algorithm pinned on verify | PASS | `claven/routes/auth.py:67-71` (HS256), `:89` `algorithms=["HS256"]` |
| 2 | Session | Session expiry / idle + absolute timeout | GAP (M2) | `auth.py:60-71` no `exp`; cookie has no `max_age` `auth.py:248` |
| 3 | Session | Logout invalidates session server-side | GAP (M2) | `auth.py:252-257` only `delete_cookie` |
| 4 | Session | Cookie HttpOnly + Secure | PASS | `auth.py:248`; OAuth cookies `auth.py:150-152` |
| 5 | Session | SameSite / anti-CSRF on state-changing requests | GAP (M3) | `samesite="none"` `auth.py:248`; bodiless POSTs `actions.py:17-84`, `api.py:508-551`; `request.json()` without Content-Type check `api.py:558,585` |
| 6 | Session | New session issued at login (no fixation) | PASS | `auth.py:244-248` |
| 7 | Access control | All `/api/*` user data scoped to session `user_id`; no client-supplied IDs | PASS | `api.py:32-45,428-429,516,543,557,584,609`; `actions.py:20-21,47-48,74-75` |
| 8 | Access control | SSE stream filtered to the session user | PASS | `api.py:472` |
| 9 | Access control | `/internal/*` requires bearer secret | PASS | `auth.py:24-27`; `internal.py:37,65,135,167`; secret from Secret Manager `deploy.yml:90` |
| 10 | Access control | Internal secret compared in constant time | GAP (should) | `auth.py:26` uses `!=`; use `secrets.compare_digest` |
| 11 | Access control | Webhook OIDC token verified, audience checked | PASS (prod) | `auth.py:30-52`; `PUBSUB_AUDIENCE` set in `deploy.yml:96`. Audience is optional in code, `auth.py:42` |
| 12 | Access control | Webhook bound to Claven's own push service account | GAP (M4) | `auth.py:54-57` accepts any `*.gserviceaccount.com`; payload `historyId` persisted `internal.py:301,313` |
| 13 | Access control | Unauthenticated endpoints expose no user data | PASS | `/health`, `/healthz` `server.py:180-199`; `/api/config` static `api.py:385-388` |
| 14 | Access control | API docs / OpenAPI schema not exposed in prod | GAP (should) | `FastAPI(title="Claven", lifespan=lifespan)` `server.py:154` leaves `/docs`, `/redoc`, `/openapi.json` on |
| 15 | Access control | Cloud Run ingress / IAM, Scheduler auth config | UNKNOWN | Not in repo beyond `scripts/setup-scheduler.sh:34,44` (bearer header) |
| 16 | OAuth | `state` random, cookie-bound, verified | PASS | `auth.py:140,150,171-174` |
| 17 | OAuth | Redirect URI fixed; post-login `return_to` allow-listed | PASS | `auth.py:102,138`; `auth.py:116-119,136-137` |
| 18 | OAuth/CORS | Prod allow-list contains only prod origins | GAP (should) | `CORS_EXTRA_ORIGINS=http://localhost:3000` in prod `deploy.yml:98`; used by CORS with credentials `server.py:156-169` and by `return_to` |
| 19 | OAuth | PKCE | GAP (nice) | `Flow.from_client_config` without `autogenerate_code_verifier` → none sent (`auth.py:95-106`). Confidential client + state, so acceptable |
| 20 | OAuth | Scope minimisation and granted-scope check | PASS | `server.py:171-175` (modify needed to apply labels/archive); `auth.py:188-191` |
| 21 | OAuth | ID token verified with client-ID audience | PASS | `auth.py:193-202` |
| 22 | OAuth | Tokens encrypted at rest | PASS | Fernet `claven/core/auth.py:36-54`; BYTEA columns `0001_initial.py:44-45` |
| 23 | OAuth | Encryption key outside DB/code | PASS | Secret Manager `deploy.yml:88`; `core/auth.py:4-6` |
| 24 | OAuth | Key rotation supported | GAP (nice) | Single key `core/auth.py:36-39`; no `MultiFernet` |
| 25 | OAuth | Tokens revoked with Google on disconnect / deletion | GAP (M5) | `api.py:536-551` keeps tokens; no `/revoke` call anywhere; `db.delete_credentials` unused `db.py:125` |
| 26 | OAuth | Revoked/invalid grants handled; account stops processing | PASS | `core/auth.py:84-108`; `0015_add_gmail_access_revoked.py` |
| 27 | Data | Only message metadata fetched (no bodies/subjects) | PASS | `gmail.py:114-138` (From/To), `gmail.py:279-280` (To/Cc/Bcc), `gmail.py:453-471`, `scan.py:427`; `format="minimal"` `api.py:123,205` |
| 28 | Data | Stored data matches privacy policy | PASS | Tables `0001_initial.py`, `0011_add_event_log.py`; policy `privacy/content.ts:58-67` |
| 29 | Data | Activity-log retention as stated ("most recent 100") | GAP (should) | `db.trim_event_log` `db.py:563` is never called, so `event_log` grows unbounded; display limit 30 `db.py:550` |
| 30 | Data | No third-party (correspondent) addresses in prod logs | GAP (should) | INFO `scan.py:275` logs `sender`; policy says only message IDs appear in logs `privacy/content.ts:69` |
| 31 | Data | User email in logs is minimised / disclosed | GAP (should) | `auth.py:239-240` (`extra.email`), `internal.py:259,263,268,278`, `api.py:527,549`; formatter emits `email` `server.py:55` |
| 32 | Data | Debug logging disabled in prod | PASS | INFO level on Cloud Run `server.py:74-78` |
| 33 | Data | User can delete account + all data | GAP (M5) | No route; email-only process `privacy/content.ts:80-82`; cascades exist `0001_initial.py:40,59,76` |
| 34 | Data | Retention of inactive / revoked accounts defined | GAP (nice) | No purge job; policy: "while your account exists" `privacy/content.ts:80` |
| 35 | Data | Prod data access from dev machines least-privilege | GAP (should) | `scripts/create_readonly_role.sql:9-13` grants SELECT on all tables, including `gmail_tokens` and `sent_recipients`, to a role used from `.env.local` |
| 36 | Data | DB TLS in transit, encryption at rest, backups | UNKNOWN | Neon setting; example uses `sslmode=require` `.env.local.example:16` |
| 37 | Injection | SQL fully parameterised | PASS | All queries in `claven/core/db.py` use `%s` params; no f-string SQL found |
| 38 | Injection | No eval/exec/shell/pickle/unsafe YAML | PASS | grep of `claven/`, `main.py`, `scripts/` found none |
| 39 | Input | Settings input allow-listed | PASS | `api.py:559-561,588-593` |
| 40 | Input | Gmail query built only from server config | PASS | `api.py:617-620` |
| 41 | XSS | Frontend escapes output; no raw HTML | PASS | No `dangerouslySetInnerHTML` in `web/`; `react-markdown` without `rehype-raw` `web/components/markdown-content.tsx` |
| 42 | Secrets | No secrets tracked; env files gitignored | PASS | `.gitignore` (`.env`, `.secrets`, `.env.local`, `client_secret*`); history grep found only local test DB creds `ci.yml:51,56` |
| 43 | Secrets | Runtime secrets from Secret Manager | PASS | `deploy.yml:86-92` |
| 44 | Secrets | CI→GCP via Workload Identity (no SA keys) | PASS | `deploy.yml:39-44`; `scripts/setup-gcp.sh:81-98` |
| 45 | Secrets | Deployer service account least-privilege | GAP (should) | `scripts/setup-gcp.sh:62-74` grants `secretmanager.admin`, `pubsub.admin`, `run.admin`, `cloudscheduler.admin` |
| 46 | Secrets | Prod secrets not exposed to every-branch CI | GAP (should) | `SESSION_SECRET` GH secret mints prod JWTs `deploy.yml:139-143,159`; same secret plus `TOKEN_ENCRYPTION_KEY` injected into CI on push to any branch `ci.yml:3-6,62-63` |
| 47 | Secrets | GitHub Actions pinned to commit SHA | GAP (nice) | Tag pins, e.g. `deploy.yml:26,29,40` |
| 48 | Transport | HTTPS-only; Secure cookies | PASS | `auth.py:150,152,248`; `wrangler.toml` custom domain; Cloud Run TLS |
| 49 | Transport | HSTS | GAP (M1) | No header in `server.py` or `web/next.config.ts` |
| 50 | Transport | CORS restricted, explicit methods/headers | PASS | `server.py:163-169` (apart from #18) |
| 51 | Headers | API: nosniff, frame-ancestors/XFO, `Cache-Control: no-store` on auth'd JSON | GAP (M1) | `server.py:154-169` |
| 52 | Headers | Frontend: CSP, XFO, Referrer-Policy, Permissions-Policy | GAP (M1) | `web/next.config.ts:3`; no middleware |
| 53 | Errors | Unhandled exceptions return generic 500 | PASS | FastAPI default; `api.py:526-528` |
| 54 | Errors | No exception text returned to clients | GAP (should) | `/healthz` returns `str(exc)` unauthenticated `server.py:198`; `error_detail=str(exc)` in redirect `auth.py:183,201,236` rendered `web/app/page.tsx:41-43`; internal `internal.py:58,224` |
| 55 | Logging | Security events logged (auth failures, access denials) | GAP (should) | OAuth failures logged `auth.py:166,174,182,190,200`, but 401s in `_get_session` `auth.py:86-90` and `_require_internal_auth` `auth.py:26-27` are not logged |
| 56 | Logging | Log retention, alerting, monitoring | UNKNOWN | Cloud Logging config not in repo |
| 57 | Dependencies | Direct Python deps pinned | PASS | `requirements.txt` (`==` on all); no hashes/lock for transitive deps |
| 58 | Dependencies | No known critical/high CVEs | GAP (M6) | `npm audit`: next 16.2.4 critical, proxy-addr critical, 20 high; starlette 0.38.6, cryptography 42.0.8 |
| 59 | Dependencies | Automated vulnerability scanning in CI | GAP (should) | No `pip-audit`/`npm audit` in `.github/workflows/*`; no `dependabot.yml` |
| 60 | Abuse | Rate limiting on auth and expensive endpoints | GAP (should) | No limiter in `claven/`; `/api/top-senders` fetches up to 500 messages per call `api.py:602-657`; `/oauth/start` open. Cap `--max-instances=10` `deploy.yml:101` |
| 61 | Abuse | Edge rate limiting / WAF (Cloudflare, Cloud Armor) | UNKNOWN | Not in repo |
| 62 | Infra | Cloud Run runtime service account and its IAM roles | UNKNOWN | Not in repo |
| 63 | Infra | MFA on GCP, GitHub, Cloudflare, Neon admin accounts | UNKNOWN | Out of repo |
| 64 | Infra | Neon app role privilege (owner vs. limited) | UNKNOWN | `DATABASE_URL` secret contents unknown |

Note: rows 49, 51 and 52 are all M1; rows 2–3 are M2; rows 25 and 33 are M5. Totals: PASS 30, GAP 27, UNKNOWN 7.

## 4. Should-fix / nice-to-have

**Should-fix (expect questionnaire findings or easy scanner hits):**
- **Error detail leakage** (#54): drop `detail: str(exc)` from `/healthz` (`server.py:198`) and stop passing `str(exc)` as `error_detail` (`auth.py:183,201,236`); log it server-side instead.
- **Constant-time compare** for `INTERNAL_API_SECRET` (`auth.py:26`) using `secrets.compare_digest`.
- **Disable OpenAPI/docs** in prod: `FastAPI(..., docs_url=None, redoc_url=None, openapi_url=None)` when `K_SERVICE` is set (`server.py:154`).
- **Remove `http://localhost:3000`** from prod `CORS_EXTRA_ORIGINS` (`deploy.yml:98`).
- **Logging PII**: downgrade `scan.py:275` to DEBUG or log a count only. Log `user_id` instead of email in `internal.py`/`api.py`/`auth.py`, or disclose email-in-logs in the privacy policy.
- **Event-log retention**: call `db.trim_event_log` after `log_event` (or add a periodic purge) so the "100 entries" statement in `privacy/content.ts:67` is true.
- **Read-only prod role**: revoke SELECT on `gmail_tokens` (and ideally `sent_recipients`) from `claven_readonly` (`scripts/create_readonly_role.sql`), or stop using prod data from laptops.
- **Deployer SA**: replace `secretmanager.admin`/`pubsub.admin` with narrower roles (`setup-gcp.sh:62-74`).
- **Separate CI secrets from prod**: use a distinct `SESSION_SECRET`/`TOKEN_ENCRYPTION_KEY` for `ci.yml` test runs, and gate the prod smoke-test secret behind a GitHub `environment` with protection.
- **Security event logging**: log 401s from `_get_session` and `_require_internal_auth` at WARNING with IP and path (no token), and add a Cloud Logging alert on spikes.
- **Rate limiting**: per-user/IP limits on `/oauth/start`, `/api/top-senders` and `/api/me` (e.g. `slowapi`, or Cloudflare rate-limit rules if the API is proxied).
- **Dependency automation**: add `dependabot.yml` and `pip-audit`/`npm audit` CI gates (part of the M6 fix).
- **Cache-Control: no-store** on authenticated JSON (part of the M1 middleware).

**Nice-to-have:**
- PKCE (`autogenerate_code_verifier=True`, with the verifier stored in an HttpOnly cookie alongside `oauth_state`).
- `MultiFernet` key rotation support in `claven/core/auth.py`.
- Pin GitHub Actions to commit SHAs.
- Hash-locked Python dependencies (`pip-compile --generate-hashes`).
- Purge job for long-revoked or inactive accounts, matching a stated retention period.
- Revoke the Google token on Disconnect, or state clearly in the UI that credentials are kept (currently only in the policy).

## 5. Non-code items the user must supply

- **CASA questionnaire answers** plus scan evidence (scan reports from the lab or tool the assessor uses after fixes).
- **Privacy policy updates**: in-app deletion and token revocation (after M5), what appears in logs (email addresses), actual log retention in Cloud Logging (policy says "normally 30 days", so confirm the bucket setting), and the event-log retention.
- **Data retention and deletion policy**: retention for active, disconnected and revoked accounts, and backup retention in Neon (PITR window).
- **Incident response plan and security contact**: who responds, notification timelines (Google requires notice of incidents involving user data), and a `security.txt`/contact address.
- **Infrastructure attestations**: Cloud Run ingress/auth settings, runtime service account roles, Neon encryption at rest/TLS and IP allow-listing, Cloudflare TLS/HSTS settings, MFA on all admin accounts (GCP, GitHub, Cloudflare, Neon, domain registrar), and who has prod access (sole operator).
- **Key management description**: where `TOKEN_ENCRYPTION_KEY`/`SESSION_SECRET` live, who can read them, and the rotation procedure.
- **Vulnerability management process**: patch cadence and how dependency alerts are triaged.
- **Scope justification**: why `gmail.modify` is needed (applying labels to messages and archiving cannot be done with `gmail.labels`/`gmail.metadata`). The demo video is already in progress, per memory.
