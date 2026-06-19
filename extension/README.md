# Palace Capture Extension

Chrome-compatible Manifest V3 extension for private dogfood capture into Palace.

The popup keeps fast local classification for UX labels, then submits the raw
browser capture contract to Palace at `/api/v1/capture/browser`:

- selected text becomes a note with source URL provenance
- YouTube, Shorts, youtu.be, and direct audio/video URLs route to media ingest
- social post URLs route through webpage ingest while preserving `capture_kind=social_post`
- ordinary `http` and `https` URLs route to webpage ingest

The settings page exchanges a Palace API key for a revocable capture token scoped
to browser capture, job-status reads, and active Web Save lookup for the popup.
The pairing key is not stored.

The popup checks the current tab against `/api/v1/web-saves` before saving. It
shows an already-saved state for exact active URL matches, keeps user-entered tags
on new captures, treats duplicate capture responses as saved/no-op, and lists a
small set of active related Web Saves from the current domain. Related results
are limited to explicit Web Save records.

## Configuration

The extension needs a Palace base URL and a temporary pairing API key during setup.

- Local default Palace URL: `https://palaceoftruth.test`
- Local API URL behind that frontend: `https://api.palaceoftruth.test`
- Pairing key input: a tenant API key with permission to request a browser-capture token
- Stored credential: the scoped capture token returned by Palace, not the pairing key

The capture token is revocable server-side and scoped to browser capture, job-status reads, and active Web Save lookup. If capture starts returning authentication errors, re-pair the extension from Settings.

## Permissions

The extension requests broad host access because it needs to classify and capture the active tab URL across ordinary web pages, media URLs, social posts, and selected text. It sends captures only after the user triggers the popup action.

## Manual QA

After building and loading `extension/dist` as an unpacked extension:

1. Pair against a local or staging Palace URL.
2. Save selected text from a normal web page and confirm the note preserves source URL provenance.
3. Save a YouTube or direct media URL and confirm it routes to media ingest.
4. Save a normal web URL and confirm the popup shows the already-saved state on the next open.
5. Archive or revoke the token server-side and confirm the popup prompts for re-pairing.

## Development

```bash
npm install
npm test
npm run build
```

Load `extension/dist` as an unpacked extension in Chrome after building.
