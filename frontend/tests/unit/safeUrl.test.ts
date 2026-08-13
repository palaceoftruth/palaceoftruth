import assert from "node:assert/strict";
import test from "node:test";

import { safeExternalUrl, safeOAuthRedirect } from "../../src/lib/safeUrl.ts";

test("external URL helper rejects active-content and parser tricks", () => {
  for (const value of [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "\u0000javascript:alert(1)",
    "data:text/html,x",
    "blob:https://example.com/id",
    "vbscript:msgbox(1)",
    "/relative",
  ]) {
    assert.equal(safeExternalUrl(value), undefined, value);
  }
  assert.equal(safeExternalUrl("https://example.com/a"), "https://example.com/a");
  assert.equal(safeExternalUrl("http://example.com/a"), "http://example.com/a");
  assert.equal(safeExternalUrl("mailto:user@example.com"), "mailto:user@example.com");
});

test("OAuth callbacks require HTTPS or loopback HTTP and no credentials", () => {
  assert.equal(safeOAuthRedirect("https://client.example/callback"), "https://client.example/callback");
  assert.equal(safeOAuthRedirect("http://127.0.0.1:9876/callback"), "http://127.0.0.1:9876/callback");
  assert.equal(safeOAuthRedirect("http://localhost:9876/callback"), "http://localhost:9876/callback");
  for (const value of [
    "http://client.example/callback",
    "javascript:alert(1)",
    "data:text/html,x",
    "https://user:pass@client.example/callback",
    "//client.example/callback",
  ]) {
    assert.equal(safeOAuthRedirect(value), undefined, value);
  }
});
