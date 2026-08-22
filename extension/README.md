# Palace Capture Extension

Chrome-compatible Manifest V3 extension for private dogfood capture into Palace.

The popup keeps fast local classification for UX labels, then submits the raw
browser capture contract to Palace at `/api/v1/capture/browser`:

- selected text becomes a note with source URL provenance
- YouTube, Shorts, youtu.be, and direct audio/video URLs route to media ingest
- social post URLs route through webpage ingest while preserving `capture_kind=social_post`
- ordinary `http` and `https` URLs route to webpage ingest
- direct image URLs upload the image file itself to `/api/v1/capture/browser/images`

## Image uploads

A URL only works for an image Palace can fetch. Anything behind a session
cookie, a signed URL, a private host, or a `blob:` source is invisible to the
server. The browser is already past all of that, so the extension reads the
bytes in the page and uploads them:

1. `fetch(url, { credentials: "include" })` inside the tab, which keeps the
   session that gated the image and usually answers from the browser cache.
2. If that is blocked, the rendered image is read back off a canvas. This
   reaches `blob:`, `data:`, and generated images, but it re-encodes to PNG:
   the result is a faithful picture, not the original file.
3. If both fail, the extension falls back to sending the URL, exactly as
   before.

Palace types the upload from the bytes, never from the file name or the
declared content type, and records the claimed source URL as a client
assertion. Nothing is fetched server-side, so uploaded images are stored under
their own `browser_image_upload` source.

Two entry points use this:

- The popup, when the active tab is a direct image URL. Images inside a social
  post are uploaded the same way, and only the ones whose bytes cannot be read
  are still sent as candidate URLs.
- The `Save image to Palace` right-click item on any image. The popup is closed
  during that save, so the toolbar badge reports the result: `OK`, `ERR`, or
  `KEY` when the extension is not paired yet.

The settings page exchanges a Palace API key for a revocable capture token scoped
to browser capture, job-status reads, and active Web Save lookup for the popup.
The pairing key is not stored.

The popup checks the current tab against `/api/v1/web-saves` before saving. It
shows an already-saved state for exact active URL matches, keeps user-entered tags
on new captures, treats duplicate capture responses as saved/no-op, and lists a
small set of active related Web Saves from the current domain. Related results
are limited to explicit Web Save records.

## Configuration

The extension needs a Palace base URL and a temporary one-time pairing key generated in Palace Settings.

- Local default Palace URL: `https://palaceoftruth.test`
- Local API URL behind that frontend: `https://api.palaceoftruth.test`
- Pairing key input: a short-lived, single-use Palace Capture pairing key
- Stored credential: the scoped capture token returned by Palace, not the pairing key

The capture token is revocable server-side and scoped to browser capture, job-status reads, and active Web Save lookup. If capture starts returning authentication errors, re-pair the extension from Settings.

## Permissions

The extension requests broad host access because it needs to classify and capture the active tab URL across ordinary web pages, media URLs, social posts, and selected text. It sends captures only after the user triggers the popup action or the right-click item.

`contextMenus` adds the `Save image to Palace` item. `scripting` is what lets
the extension read image bytes inside the tab; it runs only on the tab the user
acted on.

## Manual QA

After building and loading `extension/dist` as an unpacked extension:

1. Pair against a local or staging Palace URL.
2. Save selected text from a normal web page and confirm the note preserves source URL provenance.
3. Save a YouTube or direct media URL and confirm it routes to media ingest.
4. Save a normal web URL and confirm the popup shows the already-saved state on the next open.
5. Open a direct image URL that needs a login and confirm the popup uploads the
   file, not the URL.
6. Right-click an image inside a logged-in page, choose `Save image to Palace`,
   and confirm the badge shows `OK`.
7. Archive or revoke the token server-side and confirm the popup prompts for re-pairing.

## Development

```bash
npm install
npm test
npm run build
```

Load `extension/dist` as an unpacked extension in Chrome after building.
