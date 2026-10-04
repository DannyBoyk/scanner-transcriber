# M4 plan: search and talkgroup settings

**Status: done (2026-09-30).** All steps built; see "Build notes" at the end.

**Goal:** find any past transmission by what was said (forgiving of transcription
misspellings and partial words), filtered by talkgroup, department and time. Manage
talkgroups: a display name, colour, "record but don't transcribe", and "hide from the live
feed".

## Exit criteria

- `/search?q=…` finds transcripts by whole words, word stems ("stop" → "stops") and **partial
  words** ("Quar" → "Quarry", "1403" → "Medic 1403"). Quoted `"phrases"` match exact runs,
  `-word` excludes. Matches are highlighted. Results come newest first with "Load more".
- Filters (talkgroups, department, from/to date and time, "has audio", "include no-speech")
  combine with each other and with the query, and live in the URL (bookmarkable). A search
  box in the header on every page.
- `/channels` lists every talkgroup heard, with activity counts and last heard. Its display
  name, colour, record-only and hidden settings can be changed in place, and the change shows
  immediately in the feed, chips, search and detail pages.
- **Record-only** talkgroups are saved but never transcribed, with an option to transcribe
  their past skipped clips later. **Hidden** talkgroups are left out of the live feed (and its
  live updates) unless picked in the filter chips, but are still recorded, transcribed and
  searchable.
- Tests: search semantics (stems, partials, phrases, exclusions, filters, paging),
  highlighting, channel edits and their effects, the edit-request guard.
- Search stays fast on real data: under about 200 ms for typical queries.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Matching | **Words + partial words.** The existing FTS5 index (porter stemming) handles words/stems; a new **trigram** FTS5 index over the transcripts handles partial words of 3+ characters. No fuzzy / sounds-like matching. | Dan |
| Editable settings | **Display name (alias), colour, record-only, hidden from live feed.** | Dan |
| Priority | **Left out.** The queue has never backed up. `channels.priority` stays in the schema, unused. | Dan |
| Edit protection | **None (no login)**, as the page is only reachable from Dan's LAN and tailnet. Cheap guard against *cross-site* requests: edits must be `PATCH`/`POST` with htmx's `HX-Request` header, which another site's page can't send to the NUC without a CORS preflight the server never grants. | Dan: no protection; the guard costs nothing |
| What search covers | **Transcripts only** (primary and the second model's text). Talkgroup and department are filters, not text matches, so "sheriff" doesn't return every Sheriff transmission. | Precision |
| Sort | **Newest first** by default; "Best match" (FTS5 bm25) as an option. | It's a log |
| Access | Dan reaches the page over Tailscale via a subnet router, so the M5 `tailscale serve` step is optional. | Dan |

## Search

### Index (migration 0005)

```sql
CREATE VIRTUAL TABLE transmissions_tri USING fts5(
    transcript, alt_transcript,
    content='transmissions', content_rowid='id', tokenize='trigram'
);
-- + insert/update/delete triggers mirroring transmissions_fts, then 'rebuild'
```

It's small: transcripts are short (a few KB per 100 clips).

### Query language (simple, forgiving)

| Typed | Means |
|---|---|
| `mill road` | both words, anywhere in the transcript, in any order |
| `"traffic stop"` | that exact phrase |
| `quar` | partial word: matches "Quarry" (3+ characters) |
| `-test` | exclude transcripts containing "test" |

Each word matches if **either** index matches it: the porter index (stems: "stop", "stops",
"stopped") or the trigram index (substring, for 3+ characters). All words must match. Input is
never passed to FTS5 raw; it's parsed into quoted terms, so typos in syntax can't cause errors.
Terms shorter than 3 characters use the word index only.

### Highlighting

Done in Python after the query (FTS5's own `highlight()` doesn't line up across two indexes):
case-insensitive, marking substring hits and stem prefixes with `<mark>`. Transcripts are
short, so the whole text is shown with marks rather than a snippet.

### Page `/search`

- A **search box** in the header on every page (it submits to `/search`). On `/search`, a
  bigger box with filter controls:
  - talkgroup chips (as on the live feed, multi-select);
  - department dropdown;
  - **from / to** date-time pickers, with quick ranges: last hour, today, yesterday, last 7
    days;
  - "has audio";
  - "include no-speech" (off by default).
- Results use the **same exchange-block rows** as the live feed (colours, play buttons,
  links to detail), with `<mark>`ed matches and a count ("42 matches").
- Keyset paging, "Load more" (newest first), or offset paging when sorting by best match.
- Empty query plus filters is allowed, e.g. "all Town PD yesterday 2–4 pm".
- Every result links to its detail page, and "Show in feed" opens the live feed at that time
  (`/?at=…`).
- A friendly empty state: "No transmissions match …" with a hint to remove a filter or try a
  shorter partial word.

### API

`/api/transmissions` gains `q`, `from`, `to`, `department`, `has_audio`, `include_no_speech`,
`sort` (the design §5.7 filters), using the same code path as the page.

## Talkgroup settings

### Schema (migration 0005)

- `channels.hidden INTEGER NOT NULL DEFAULT 0`: left out of the live feed.
- Existing columns reused: `alias` (display name), `color` (palette index 0–9), `ignored`
  (record, don't transcribe; M2 already skips these at insert).

### Page `/channels`

One row per talkgroup ever heard, sortable by last heard (default) or busiest:

- colour swatch, display name (alias, else channel), department, TGID;
- activity: last hour, today, last 7 days, total; last heard ("3m ago");
- settings, edited in place (htmx; saved on change, with a small "Saved" confirmation):
  - **display name**: text field; empty = the scanner's own name;
  - **colour**: the 10-colour palette as swatches;
  - **Transcribe**: on/off. Off = record only. Turning it back on offers "Transcribe the N
    clips recorded while off"; they're queued oldest first;
  - **Show in live feed**: on/off (hidden);
- a link to that talkgroup's recent transmissions (`/?tg=…`) and to search within it.

### Effects

- **Alias** and **colour**: the feed, chips, search, detail and new SSE rows all read from
  `channels`, so they change on the next render. Nothing is copied into old rows.
- **Record-only** (`ignored`): new clips are saved as `skipped` (existing behaviour). Clips
  already queued when it's switched on are left alone. Re-queueing past skipped clips is
  explicit.
- **Hidden:** the live feed's initial query, "Load older" and the SSE change feed exclude
  hidden talkgroups **unless** the talkgroup filter (`?tg=`) names them. Hidden talkgroups
  still appear as chips, dimmed, so they can be picked. Search and detail pages are
  unaffected.

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/channels` | List with settings and activity |
| PATCH | `/api/channels/{key}` | Update `alias`, `color`, `ignored`, `hidden` (requires `HX-Request` or JSON content type) |
| POST | `/api/channels/{key}/transcribe-skipped` | Re-queue that talkgroup's skipped clips |

## Tests

- **Search:** stems, partial words (including numbers), phrases, exclusions, AND across
  words, short terms, filters (talkgroups, department, date range, has audio, no-speech),
  keyset paging with no gaps or duplicates, "best match" order, empty query plus filters, and
  malicious or odd input (quotes, `*`, `NEAR`, SQL-looking text) with no errors.
- **Highlighting:** substring and stem marks, HTML-escaping of transcript text (no injection
  via transcripts).
- **Trigram index:** stays in sync on insert, update and delete (triggers).
- **Channels:** list and activity counts; PATCH each setting and see it on the feed, chips,
  SSE rows and search; the edit guard (a plain cross-site-style form POST without the header
  → 403); record-only → new clips skipped; re-queue skipped; hidden → excluded from live
  feed and SSE but present in search and when explicitly filtered.
- **Browser (tests/ui):** search as you'd use it on phone and desktop; editing a colour and
  alias updates the live page after reload.
- **Real data:** timing of typical searches on the current DB.

## Order of work

1. Migration 0005 (trigram index + triggers, `channels.hidden`), then the search query builder
   and highlighter, with unit tests.
2. `/search` page + header search box + API filters.
3. `/channels` page with in-place editing, the PATCH/POST endpoints and the edit guard.
4. Effects wiring: hidden in the feed/SSE/chips, re-queue skipped, alias/colour everywhere.
5. Tests, browser checks, real-data timing; docs.

## Build notes (2026-09-30)

- **Search:** `web/search.py` parses input into terms, never raw FTS5 syntax. Each term
  matches the porter index (column-filtered to the transcript columns) OR the trigram index
  (3+ characters). Keyset paging, newest first. **"Best match" sorting was dropped:** scores
  from two combined indexes aren't comparable, and a log reads newest first. Highlighting is
  done in Python and HTML-escaped (tested against injected markup in transcripts).
- `/search` has a header box on every page (hidden on phones, where the Search tab covers
  it), from/to pickers plus Last hour / Today / Yesterday / Last 7 days, a department
  dropdown, has-audio / include-no-speech, and talkgroup chips across all talkgroups. Links
  are built server-side. Real data: about 10 ms per query (373 rows).
- **Talkgroups page** (`/channels`, nav label "Talkgroups"): per-talkgroup cards with
  activity (last hour / today / 7 days, last heard), display name (saves on change), a
  10-colour picker, toggle buttons "Transcribe: on/off" and "Live feed: shown/hidden", and
  "Transcribe the N clips recorded while off" when relevant. Each row redraws itself with
  "Saved".
- **Edit guard:** PATCH/POST require `HX-Request: true` or a JSON body; plain cross-site form
  posts get 403. htmx forms are parsed with `urllib.parse.parse_qs` (no `python-multipart`
  dependency). Talkgroup keys contain `/`, so the routes use `{key:path}`.
- **Hidden talkgroups** are filtered in `feed._where` (initial page, load older/newer, jump-to)
  and in `ChangeFeed` (live updates) unless `?tg=` names them. Their chips are dimmed and
  dashed. Search and detail pages are unaffected.
- **Tests:** 184 pytest (search semantics and odd input, highlighting and escaping, trigram
  sync, search page, channel edits and effects, guard). Browser: `tests/ui/channels_check.py`
  (7 checks: alias, colour, hide, feed effect). Visual check with headless Firefox (desktop
  light, phone dark).

## Later (not M4)

- **Keyword alerts** ("notify me when 'structure fire' is said"): natural next step. It would
  hang off the transcription worker plus the M5 notification idea.
- Export search results (CSV/JSON), from the design's later list.
- Fuzzy / sounds-like search, if partial words turn out not to be enough.
