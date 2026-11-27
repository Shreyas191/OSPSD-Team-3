"""Sign in with Google and connect providers against a locally running API, from a terminal.

It plays the interface's part (#94/#95 aren't built yet), so sign-in (#96) and provider
connections (#98-#101) can be tried end to end with real Google, Dropbox and Trello:

1. starts a callback server on ``http://localhost:8765``;
2. asks the API for a Google sign-in, opens the browser, catches Google's redirect and
   finishes the sign-in. The session token stays in this process's memory;
3. for each ``--connect`` provider, does the same with that provider. Trello returns its token
   in the URL fragment, which no server sees, so the callback page posts it back here;
4. lists the connections, reads ``/courses`` as the signed-in user, and signs out.

No token is printed, written to a file or put in a URL this script controls.

**Setup, once.** Add these redirect URIs to the OAuth apps (only the ones you'll connect):

- Google OAuth client (sign-in and Calendar): ``http://localhost:8765/callback`` and
  ``http://localhost:8765/connections/google_calendar/callback``;
- Dropbox app: ``http://localhost:8765/connections/dropbox/callback``;
- Trello Power-Up: allowed origin ``http://localhost:8765``.

**Run the API** in one terminal (secrets go straight from Secret Manager into the
environment; a local SQLite file stands in for Neon)::

    export GOOGLE_OAUTH_CLIENT_ID="$(gcloud secrets versions access latest --secret=google-oauth-client-id)"
    export GOOGLE_OAUTH_CLIENT_SECRET="$(gcloud secrets versions access latest --secret=google-oauth-client-secret)"
    export SIGN_IN_REDIRECT_URI=http://localhost:8765/callback
    export CALENDAR_CONNECT_REDIRECT_URI=http://localhost:8765/connections/google_calendar/callback
    export DROPBOX_CONNECT_REDIRECT_URI=http://localhost:8765/connections/dropbox/callback
    export TRELLO_CONNECT_API_KEY=<the Power-Up's API key>
    export TRELLO_CONNECT_RETURN_URL=http://localhost:8765/connections/trello/callback
    export TOKEN_ENCRYPTION_KEY="$(openssl rand -base64 32 | tr '+/' '-_')"
    export DATABASE_URL=sqlite:///./local-sign-in.db
    uv run uvicorn cloud_storage_service.app:app --port 8000

(``gcloud`` with ``CLOUDSDK_CONFIG=$HOME/.config/gcloud-ospsd --project ospsd-team3``.) The
API also needs its usual Dropbox settings to start (``.env`` / ``.dropbox_token.json``).

**Then, in another terminal**::

    uv run python scripts/local_sign_in.py --connect google_calendar dropbox trello

``--keep`` skips the sign-out at the end. The local database keeps the user and the
encrypted connections; delete ``local-sign-in.db`` when done.
"""

# ruff: noqa: T201, INP001 - a command-line script, not part of a package
import argparse
import json
import queue
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

PROVIDERS = ("google_calendar", "dropbox", "trello")
WAIT_SECONDS = 300

# Trello puts the token in the fragment (#token=...). This page reads it in the browser and
# posts it to the script, with the state from the query, so it never sits in a URL.
TRELLO_PAGE = b"""<!doctype html><meta charset="utf-8"><title>Connecting Trello</title>
<p id="m">Connecting Trello...</p>
<script>
const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
const state = new URLSearchParams(location.search).get("state") || "";
history.replaceState(null, "", location.pathname);
fetch("/connections/trello/token", {method: "POST", body: JSON.stringify({state, token})})
  .then(() => { document.getElementById("m").textContent = "Done; return to the terminal."; });
</script>"""
DONE_PAGE = b"""<!doctype html><meta charset="utf-8"><title>Done</title>
<p>Done; return to the terminal.</p>"""


class Callbacks:
    """A local server that hands each provider's redirect to whoever is waiting for it."""

    def __init__(self, port: int) -> None:
        """Start serving on ``localhost:port`` in a background thread."""
        self.results: dict[str, queue.Queue[dict[str, str]]] = {}
        callbacks = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                params = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path == "/connections/trello/callback":
                    self._page(TRELLO_PAGE)
                    return
                if parts.path in {"/callback", *(f"/connections/{p}/callback" for p in PROVIDERS)}:
                    callbacks.deliver(parts.path, params)
                    self._page(DONE_PAGE)
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                if urlsplit(self.path).path != "/connections/trello/token":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                callbacks.deliver(
                    "/connections/trello/callback",
                    {"state": str(body.get("state", "")), "code": str(body.get("token", ""))},
                )
                self._page(b"ok")

            def _page(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401, ARG002
                return  # The query holds the authorization code: keep it off the terminal.

        self.server = HTTPServer(("localhost", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def expect(self, path: str) -> "queue.Queue[dict[str, str]]":
        """Start waiting for a redirect to ``path``."""
        self.results[path] = queue.Queue(maxsize=1)
        return self.results[path]

    def deliver(self, path: str, params: dict[str, str]) -> None:
        """Hand a redirect to its waiter, if any."""
        waiting = self.results.get(path)
        if waiting is not None and waiting.empty():
            waiting.put(params)

    def close(self) -> None:
        """Stop serving."""
        self.server.shutdown()


def fail(response: httpx.Response, step: str) -> None:
    """Stop with the API's error code and message (never a token)."""
    try:
        error = response.json()["error"]
        detail = f"{error['code']}: {error['message']}"
    except (ValueError, KeyError, TypeError):
        detail = f"HTTP {response.status_code}"
    sys.exit(f"{step} failed: {detail}")


def wait(results: "queue.Queue[dict[str, str]]", what: str) -> dict[str, str]:
    """Wait for the browser to come back."""
    try:
        params = results.get(timeout=WAIT_SECONDS)
    except queue.Empty:
        sys.exit(f"No {what} redirect within {WAIT_SECONDS} s")
    if "error" in params:
        sys.exit(f"{what} was refused: {params['error']}")
    return params


def sign_in(api: httpx.Client, callbacks: Callbacks) -> dict[str, str]:
    """Sign in with Google; return the bearer header for the new session."""
    start = api.post("/auth/google/start")
    if start.status_code != httpx.codes.OK:
        fail(start, "Starting sign-in")
    body = start.json()
    results = callbacks.expect("/callback")
    print("Opening Google sign-in in your browser...")
    webbrowser.open(body["authorization_url"])
    params = wait(results, "Google sign-in")
    finish = api.post(
        "/auth/google/finish",
        json={"state": params.get("state", ""), "code": params.get("code", ""), "binding": body["binding"]},
    )
    if finish.status_code != httpx.codes.OK:
        fail(finish, "Finishing sign-in")
    session = finish.json()
    print(f"Signed in as {session['user']['display_name']!r} (user {session['user']['id']})")
    return {"Authorization": f"Bearer {session['session_token']}"}


def connect(api: httpx.Client, callbacks: Callbacks, who: dict[str, str], provider: str) -> None:
    """Connect one provider as the signed-in user."""
    start = api.post(f"/connections/{provider}/start", headers=who)
    if start.status_code != httpx.codes.OK:
        fail(start, f"Starting {provider}")
    results = callbacks.expect(f"/connections/{provider}/callback")
    print(f"Opening {provider} in your browser...")
    webbrowser.open(start.json()["authorization_url"])
    params = wait(results, provider)
    finish = api.post(
        f"/connections/{provider}/finish",
        json={"state": params.get("state", ""), "code": params.get("code", "")},
        headers=who,
    )
    if finish.status_code != httpx.codes.OK:
        fail(finish, f"Finishing {provider}")
    view = finish.json()
    print(f"  {provider}: {view['status']}, account {view['account']!r}")
    if provider == "trello" and view["status"] == "setup_needed":
        choose_trello_list(api, who)


def choose_trello_list(api: httpx.Client, who: dict[str, str]) -> None:
    """Ask which Trello list the cards go to."""
    lists = api.get("/connections/trello/lists", headers=who)
    if lists.status_code != httpx.codes.OK:
        fail(lists, "Listing Trello lists")
    found = lists.json()
    if not found:
        sys.exit("Your Trello account has no open lists; create one, then run this again")
    for number, item in enumerate(found, 1):
        print(f"    {number}. {item['board']} / {item['name']}")
    choice = int(input("  Which list should cards go to? ") or "1")
    chosen = api.put("/connections/trello/list", json={"list_id": found[choice - 1]["id"]}, headers=who)
    if chosen.status_code != httpx.codes.OK:
        fail(chosen, "Choosing the Trello list")
    print(f"  trello: {chosen.json()['status']}")


def main() -> None:
    """Sign in, connect what was asked, show the result, sign out."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="the local API service")
    parser.add_argument("--port", type=int, default=8765, help="the callback server's port")
    parser.add_argument("--connect", nargs="*", choices=PROVIDERS, default=[])
    parser.add_argument("--keep", action="store_true", help="don't sign out at the end")
    args = parser.parse_args()

    callbacks = Callbacks(args.port)
    try:
        with httpx.Client(base_url=args.api, timeout=30) as api:
            who = sign_in(api, callbacks)
            me = api.get("/auth/me", headers=who).json()
            print(f"Session accepted: scope {me['scope']}")
            for provider in args.connect:
                connect(api, callbacks, who, provider)
            print("Connections:")
            for view in api.get("/connections", headers=who).json():
                print(f"  {view['provider']}: {view['status']} {view['account']!r}")
            courses = api.get("/courses", headers=who)
            print(f"GET /courses as you: HTTP {courses.status_code} {courses.text[:200]}")
            if not args.keep:
                out = api.post("/auth/sign-out", headers=who)
                after = api.get("/auth/me", headers=who)
                print(f"Signed out: HTTP {out.status_code}; the session now gets HTTP {after.status_code}")
    finally:
        callbacks.close()


if __name__ == "__main__":
    main()
