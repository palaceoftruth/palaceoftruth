/**
 * Reading the bytes of an image the browser has already loaded.
 *
 * Sending Palace a URL only works for images Palace itself can fetch. Anything
 * behind a session cookie, a signed URL, a private host, or a `blob:`/`data:`
 * source is invisible to the server. The browser is already past all of that,
 * so the extension reads the bytes here and uploads them instead.
 */

export type ImageByteOrigin = "page_fetch" | "canvas" | "context_menu";

export type GrabbedImageBytes = {
  base64: string;
  mediaType: string;
  byteSize: number;
  origin: ImageByteOrigin;
  width?: number;
  height?: number;
  altText?: string;
};

/** Mirrors the server's upload ceiling, so a doomed upload is not attempted. */
export const MAX_UPLOAD_IMAGE_BYTES = 8 * 1024 * 1024;

export const SUPPORTED_UPLOAD_MEDIA_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp"];

/**
 * Runs inside the page through `chrome.scripting.executeScript`, so it must
 * stay self-contained: the function is serialized, and nothing it closes over
 * travels with it.
 *
 * Returns `null` whenever the bytes cannot be read. Callers fall back to the
 * URL-only capture path rather than failing the save.
 */
export async function grabImageBytesInPage(
  imageUrl: string,
  maxBytes: number,
): Promise<GrabbedImageBytes | null> {
  const supportedMediaTypes = ["image/jpeg", "image/png", "image/gif", "image/webp"];

  function toBase64(bytes: Uint8Array): string {
    // Chunked because String.fromCharCode is spread across arguments, and a
    // whole image at once overflows the argument limit.
    let binary = "";
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return btoa(binary);
  }

  function describe(image: HTMLImageElement | null): Partial<GrabbedImageBytes> {
    if (!image) return {};
    const alt = image.alt?.trim();
    return {
      width: image.naturalWidth || undefined,
      height: image.naturalHeight || undefined,
      altText: alt ? alt.slice(0, 300) : undefined,
    };
  }

  function matchingImage(): HTMLImageElement | null {
    const images = Array.from(document.images ?? []);
    return images.find((image) => image.currentSrc === imageUrl || image.src === imageUrl) ?? null;
  }

  async function fromBlob(blob: Blob, origin: ImageByteOrigin): Promise<GrabbedImageBytes | null> {
    const mediaType = (blob.type || "").split(";")[0].trim().toLowerCase();
    if (!supportedMediaTypes.includes(mediaType)) return null;
    if (blob.size > maxBytes) return null;
    const bytes = new Uint8Array(await blob.arrayBuffer());
    if (!bytes.length || bytes.length > maxBytes) return null;
    return { base64: toBase64(bytes), mediaType, byteSize: bytes.length, origin };
  }

  // 1. Re-request the image from the page's own origin. The session that
  //    gated it still applies, and the browser cache normally answers without
  //    a second trip to the network.
  try {
    const response = await fetch(imageUrl, { credentials: "include", cache: "force-cache" });
    if (response.ok) {
      const grabbed = await fromBlob(await response.blob(), "page_fetch");
      if (grabbed) return { ...grabbed, ...describe(matchingImage()) };
    }
  } catch {
    // A cross-origin host without CORS, a revoked blob: URL, or an offline
    // tab all land here. The canvas below is the remaining option.
  }

  // 2. Read back what the page already rendered. This reaches `blob:`,
  //    `data:`, and generated images, but it re-encodes: the result is a
  //    faithful picture, not the original file. A cross-origin image with no
  //    CORS headers taints the canvas and throws instead.
  const image = matchingImage();
  if (!image || !image.naturalWidth || !image.naturalHeight) return null;
  try {
    const canvas = document.createElement("canvas");
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d");
    if (!context) return null;
    context.drawImage(image, 0, 0);
    const blob = await new Promise<Blob | null>((resolve) => {
      canvas.toBlob((value) => resolve(value), "image/png");
    });
    if (!blob) return null;
    const grabbed = await fromBlob(blob, "canvas");
    return grabbed ? { ...grabbed, ...describe(image) } : null;
  } catch {
    return null;
  }
}

/** Turns the base64 an injected grab returned back into bytes. */
export function decodeGrabbedImage(grabbed: GrabbedImageBytes): Uint8Array<ArrayBuffer> {
  const binary = atob(grabbed.base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

/**
 * Reads one image's bytes out of a tab. Returns `null` when the tab cannot be
 * scripted or the bytes are unreadable, which is a fallback signal, not an
 * error to surface.
 */
export async function grabImageBytesFromTab(
  tabId: number,
  imageUrl: string,
): Promise<GrabbedImageBytes | null> {
  if (typeof chrome === "undefined" || !chrome.scripting?.executeScript) return null;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: grabImageBytesInPage,
      args: [imageUrl, MAX_UPLOAD_IMAGE_BYTES],
    });
    return results[0]?.result ?? null;
  } catch {
    return null;
  }
}
