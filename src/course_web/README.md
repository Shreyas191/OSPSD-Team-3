# course-web

The interface for Course Autopilot (HW3 Milestone 2, Level 2; DESIGN.md decision P4): a small
FastAPI app that renders pages with Jinja templates. It holds no product rule and calls no
provider or database; pages get their data from the agent and API services over HTTP.

Deployed as `course-web-<env>` by OpenTofu (`infra/web.tf`) and the pipeline
(`scripts/deploy_web.sh`). So far it has the start page, Google sign-in and sign-out, and the
signed-in course list (#95, first slice). Until the person's Dropbox is connected and usable,
the signed-in page says what to connect and links to `/connections`, where the person
connects, reconnects or disconnects Dropbox, Google Calendar and Trello, and chooses the
Trello list their cards go to.

"Ask the assistant" starts a conversation (`/chat/<id>`). Each message is one turn at the
agent service, sent with an agent token the interface makes for that turn from the session
(`POST /auth/agent-token`: read and propose only, 5 minutes); the session token never goes to
the agent. The page shows the person's messages and the assistant's replies as plain text,
not the agent's tool calls, and links a reply that prepared changes to `/proposals/<id>`.

The proposal page shows every change exactly as stored: the event's time in the zone the
change states, the card, and the document's full text. Approving sends the `changes_hash` the
page showed, so changed content is never approved, then makes the changes and shows each
one's outcome. Unchecking some changes approves nothing: that proposal is rejected and a new
one with the kept changes is shown for review. A failed proposal can be retried, and a change
marked "Not sure it happened" can be settled once the person has checked
(`.../changes/<index>/resolve`, with the record's ID when it was created). Approving,
rejecting, running and settling use the person's session; an agent token can't do any of them.

Each provider sends the browser back to `<PUBLIC_URL>/connections/<provider>/callback`
(`dropbox`, `google_calendar`, `trello`), so those are the URLs to set in the API service's
`connections` variable and to register with Dropbox, the Google OAuth client and the Trello
Power-Up. Trello returns its token in the URL fragment, which no server sees, so its callback
page loads `static/trello-return.js`, which posts the token back in a form with the CSRF value.

Every form carries a CSRF value that must match the browser's `ca_csrf` cookie. The session
token lives in the `ca_session` cookie (`HttpOnly`, `SameSite=Lax`, `Secure` when deployed)
and goes to the API service as `Authorization: Bearer`. Google sends the browser back to
`<PUBLIC_URL>/auth/callback`, so that URL must be in the API service's `sign_in_redirect_uris`.

Pages can't be framed by another site (`frame-ancestors 'none'` and `X-Frame-Options: DENY`),
and the Content-Security-Policy allows nothing inline: styles come from `static/site.css`,
and scripts from `static/trello-return.js` and `static/busy.js` (which says a long turn or
run is working and stops a second click from sending the form again). Forms may lead only to this site, Google,
Dropbox and Trello (`form-action`), since connecting redirects the form's post there.

```bash
uv run python web_main.py        # http://127.0.0.1:8002
```

| Setting | Meaning | Default |
|---|---|---|
| `HOST`, `PORT` | Address to serve on | `127.0.0.1`, `8002` |
| `API_BASE_URL` | The API service. Without it, the start page says sign-in isn't open yet | none |
| `AGENT_BASE_URL` | The agent service. Without it, a conversation shows but has no message box | none |
| `PUBLIC_URL` | The interface's one public address. Page requests on another address (except a revision's tag URL and `/health`) are redirected there | none |
| `INSECURE_COOKIES` | `enabled` drops the cookies' `Secure` flag for `http://localhost`; refused on Cloud Run | off |
| `RELEASE` | Commit shown in the page footer and `/health` (set by the image build) | `unknown` |
