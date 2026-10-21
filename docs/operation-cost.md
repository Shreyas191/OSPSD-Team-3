# Operation Cost: Listing a Folder (`GET /folders/list`)

`GET /folders/list?path=/Docs` returns every direct child of a folder (files and subfolders, not their contents) as a JSON array of `File` objects. Its work grows with the number of entries in the folder, which makes it the most input-dependent operation in the service.

## The path of one request

```
GET /folders/list?path=/Docs
  │  FastAPI checks the query against the path pattern                  local, O(1)
  ▼
list_folder route ── entries = list(client.list_folder(path)) ──────────► collects all n entries
  │
  ▼
DropboxClient.list_folder (a generator)
  │  files_list_folder(path)                      ──► Dropbox, page 1
  │  for each entry: yield DropboxFile(entry)
  │  while result.has_more:
  │      files_list_folder_continue(cursor)       ──► Dropbox, page 2 … k
  │      for each entry: yield DropboxFile(entry)
  ▼
route builds [FileResponse.from_file(e) for e in entries], FastAPI serializes to JSON
```

## Definitions and assumptions

- **n:** the number of direct children of the folder.
- **P:** the number of entries Dropbox returns per page. We don't pass Dropbox's `limit` parameter, so Dropbox chooses the page size. Its documentation describes `limit` as approximate, with a maximum of 2000 per request. **Assumption:** pages are close to full, so P is roughly constant, on the order of hundreds to 2000.
- **k:** the number of pages, so the number of Dropbox calls.
- Each entry is small and bounded: a name, a path, an ID, and for files a size and modification time. **Assumption:** paths have bounded length, so handling one entry is O(1).
- Listing is not recursive: subfolder contents are not fetched.

## Entries returned vs. entries fetched

For this operation the two are the same: **every entry Dropbox returns is passed to the caller.** We don't filter anything locally, and Dropbox excludes deleted entries by default. So entries fetched = entries inspected = entries returned = n.

`search` is different, for contrast. It asks Dropbox for up to `max_results` matches per page, skips matches that aren't file or folder metadata, and stops as soon as it has yielded `max_results`. There, entries fetched ≥ entries returned, and the last page may be partly unused.

## Cost

| Resource | Cost | Where it comes from |
|---|---|---|
| **Dropbox calls** | k = max(1, ⌈n / P⌉) in the typical case; at least 1 even for an empty folder | One `files_list_folder`, then one `files_list_folder_continue` per further page. The calls are **sequential**: each needs the cursor from the previous response, so they can't run in parallel. |
| **Local time** | O(n) | SDK parsing of each page's JSON (O(P) per page), one `DropboxFile` wrapper per entry (O(1)), one `FileResponse` per entry (O(1)), and JSON serialization of the response (O(n)). |
| **Local memory** | O(n) | The generator only holds one page (O(P)) at a time, but the route calls `list(...)` and then builds a second list of `FileResponse` objects, so all n entries are held at once, plus the O(n) response body. |
| **Response size** | O(n) | One JSON object per entry; nothing is paginated toward the caller. |
| **Wall-clock latency** | about k × (Dropbox round trip + Dropbox's server-side time) + O(n) local work | Dominated by the k sequential round trips for any real folder. |

Example, under the assumption P ≈ 2000: a folder with 10 entries takes 1 Dropbox call. A folder with 10,000 entries takes about 5 sequential calls and holds about 10,000 `FileResponse` objects in memory before responding.

## Work done by Dropbox, and what we can't know

Dropbox does the actual folder lookup, decides the page size, and builds each page and cursor. We can't see how that is implemented. We don't know whether Dropbox's server-side time per page depends on folder size, how cursors are stored, or whether repeated listings are cached. One more consequence: Dropbox may return a short or even empty page with `has_more = true`. Our loop handles that correctly by following the cursor until `has_more` is false. In that case k can exceed ⌈n / P⌉, so ⌈n / P⌉ is a lower bound, not a guarantee.

A slow listing therefore doesn't by itself show where the time went. Local work (parsing and serialization, both O(n)) could be the cause, but so could network latency to Dropbox (k round trips) or Dropbox's own server time, which we can't observe. Finding out would mean timing each `files_list_folder*` call separately from the route's local work. We haven't measured that; this analysis is based on the code and Dropbox's documentation.

## What would change the cost

- **Paginating the HTTP response** (passing Dropbox's cursor through to the caller) would bound memory per request at O(P) and let callers stop early. That would change the API contract, and it has no Dropbox-independent design yet.
- **Streaming the JSON response** from the generator would bound memory without changing the contract, but the response could no longer report a Dropbox failure on page 2 with an error status, because the status line would already have been sent.
- **Passing a larger `limit`** might reduce k for big folders. Since Dropbox calls the limit approximate, we'd need to measure it before relying on it.
