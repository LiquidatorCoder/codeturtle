# Security notes for codeturtle

<!-- Copy to .github/codeturtle-security.md. codeturtle reads it from the base branch and gives it to
     the security pass and the verifier as the owner's threat model. Keep it short and factual: what
     the trust boundaries are, not general advice. Replace every example below. -->

## Who can do what

- Anonymous visitors: the marketing pages and `POST /api/signup` only.
- Signed-in users: their own projects. A project belongs to exactly one workspace.
- Workspace admins: invite and remove members, change billing.
- Internal services: only the billing worker may call `/internal/*`, using the `X-Service-Token` check
  in `src/middleware/service.ts`.

## Boundaries that must hold

- Workspaces never share rows: every query on `projects`, `files` and `invites` filters by
  `workspace_id` from the session, never from the request body.
- File downloads go through signed URLs that expire after 10 minutes.
- Webhooks from Stripe are verified with the raw body in `src/webhooks/stripe.ts`.

## Facts outside this repository

- Cloudflare sits in front of the API and strips `X-Forwarded-*` from client requests.
- The production database user has no `DROP` or `ALTER` rights.

## Not a concern here

- The admin panel is only reachable on the company VPN.
