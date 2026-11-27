# course-web

The interface for Course Autopilot (HW3 Milestone 2, Level 2; DESIGN.md decision P4): a small
FastAPI app that renders pages with Jinja templates. It holds no product rule and calls no
provider or database; pages get their data from the agent and API services over HTTP.

Deployed as `course-web-<env>` by OpenTofu (`infra/web.tf`) and the pipeline
(`scripts/deploy_web.sh`). So far it has the start page, Google sign-in and sign-out, and the
signed-in course list (#95, first slice). Until the person's Dropbox is connected and usable,
the signed-in page says what to connect instead of showing an error. Conversations, proposal
review and outcomes come next.

Every form carries a CSRF value that must match the browser's `ca_csrf` cookie. The session
token lives in the `ca_session` cookie (`HttpOnly`, `SameSite=Lax`, `Secure` when deployed)
and goes to the API service as `Authorization: Bearer`. Google sends the browser back to
`<PUBLIC_URL>/auth/callback`, so that URL must be in the API service's `sign_in_redirect_uris`.

Pages can't be framed by another site (`frame-ancestors 'none'` and `X-Frame-Options: DENY`),
and the Content-Security-Policy allows nothing inline: styles come from `static/site.css`.

```bash
uv run python web_main.py        # http://127.0.0.1:8002
```

| Setting | Meaning | Default |
|---|---|---|
| `HOST`, `PORT` | Address to serve on | `127.0.0.1`, `8002` |
| `API_BASE_URL` | The API service. Without it, the start page says sign-in isn't open yet | none |
| `PUBLIC_URL` | The interface's one public address. Page requests on another address (except a revision's tag URL and `/health`) are redirected there | none |
| `INSECURE_COOKIES` | `enabled` drops the cookies' `Secure` flag for `http://localhost`; refused on Cloud Run | off |
| `RELEASE` | Commit shown in the page footer and `/health` (set by the image build) | `unknown` |
