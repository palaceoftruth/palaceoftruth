/**
 * Scheme allow-list for URLs that reach an `href` binding.
 *
 * React does not sanitize `href`, so a `javascript:` or `data:` URL bound there
 * executes on click. Every URL rendered by the app is server-supplied, and the
 * backend validates it today - but the guard belongs next to the sink as well,
 * not only in a different layer.
 */
const ALLOWED_PROTOCOLS = new Set(["https:", "http:", "mailto:"]);

/**
 * Return `value` when it is a URL that is safe to put in an `href`, otherwise
 * `undefined`. Relative URLs are rejected here on purpose: this helper is for
 * externally-supplied links, and in-app navigation goes through the router.
 */
export function safeExternalUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  let parsed: URL;
  try {
    // No base URL: relative values throw here and are rejected. The parser also
    // strips embedded tabs and newlines, so `java\tscript:` cannot slip past
    // the protocol check below.
    parsed = new URL(trimmed);
  } catch {
    return undefined;
  }
  if (!ALLOWED_PROTOCOLS.has(parsed.protocol)) return undefined;
  return trimmed;
}

/** Accept OAuth callbacks only over HTTPS or loopback HTTP. */
export function safeOAuthRedirect(value: unknown): string | undefined {
  if (typeof value !== "string" || !value.trim()) return undefined;
  let parsed: URL;
  try {
    parsed = new URL(value.trim());
  } catch {
    return undefined;
  }
  if (parsed.username || parsed.password) return undefined;
  const loopback = parsed.hostname === "127.0.0.1" || parsed.hostname === "[::1]" || parsed.hostname === "localhost";
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && loopback)) return undefined;
  return parsed.toString();
}
