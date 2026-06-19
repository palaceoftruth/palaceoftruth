export type BrowserImageCandidate = {
  url: string;
  source_post_url?: string;
  alt_text?: string;
  width?: number;
  height?: number;
  role?: string;
  order?: number;
};

export function extractXPostImageCandidates(sourcePostUrl?: string | null): BrowserImageCandidate[] {
  const maxCandidates = 4;
  const maxImagesToInspect = 80;
  const minUsefulDimension = 48;
  const maxAltTextLength = 300;

  function normalizedHttpUrl(value: string | null | undefined): string | null {
    if (!value?.trim()) return null;
    try {
      const parsed = new URL(value.trim(), globalThis.location?.href);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
      return parsed.href;
    } catch {
      return null;
    }
  }

  function sourceStatusId(value: string | null | undefined): string | null {
    const url = normalizedHttpUrl(value);
    if (!url) return null;
    const match = new URL(url).pathname.match(/\/status\/(\d+)/i);
    return match?.[1] ?? null;
  }

  function isXStatusPage(value: string | null | undefined): boolean {
    const url = normalizedHttpUrl(value);
    if (!url) return false;
    const parsed = new URL(url);
    const isXHost = /(^|\.)x\.com$/i.test(parsed.hostname) || /(^|\.)twitter\.com$/i.test(parsed.hostname);
    return isXHost && sourceStatusId(url) !== null;
  }

  function imageRole(image: HTMLImageElement): string | null {
    return image.getAttribute("role") ?? image.closest("[role]")?.getAttribute("role") ?? null;
  }

  function renderedDimension(image: HTMLImageElement, axis: "width" | "height"): number | null {
    const rect = typeof image.getBoundingClientRect === "function" ? image.getBoundingClientRect() : null;
    const rendered = axis === "width" ? rect?.width : rect?.height;
    const natural = axis === "width" ? image.naturalWidth : image.naturalHeight;
    const fallback = axis === "width" ? image.width : image.height;
    const value = Math.round(rendered || natural || fallback || 0);
    return value > 0 ? value : null;
  }

  function isLikelyExcludedImage(image: HTMLImageElement, url: string, width: number | null, height: number | null): boolean {
    const lowerUrl = url.toLowerCase();
    const alt = (image.alt ?? "").toLowerCase();
    const className = typeof image.className === "string" ? image.className.toLowerCase() : "";
    const testId =
      image.getAttribute("data-testid") ??
      image.closest("[data-testid]")?.getAttribute("data-testid") ??
      "";
    const lowerTestId = testId.toLowerCase();

    if (url.startsWith("data:") || url.startsWith("blob:")) return true;
    if (lowerUrl.includes("/profile_images/") || lowerUrl.includes("/profile_banners/")) return true;
    if (lowerUrl.includes("emoji") || lowerUrl.includes("/hashflag/")) return true;
    if (lowerUrl.includes("analytics") || lowerUrl.includes("pixel")) return true;
    if (alt.includes("avatar") || alt.includes("profile picture") || alt.includes("emoji")) return true;
    if (className.includes("avatar") || className.includes("emoji")) return true;
    if (lowerTestId.includes("avatar") || lowerTestId.includes("emoji")) return true;
    if ((width !== null && width < minUsefulDimension) || (height !== null && height < minUsefulDimension)) return true;
    return false;
  }

  function postArticleRoot(): ParentNode | null {
    const doc = globalThis.document;
    if (!doc) return null;
    const wantedStatusId = sourceStatusId(sourcePostUrl ?? globalThis.location?.href);
    const articles = Array.from(doc.querySelectorAll<HTMLElement>('article, [role="article"]'));
    if (wantedStatusId) {
      for (const article of articles) {
        const statusLinks = Array.from(article.querySelectorAll<HTMLAnchorElement>('a[href*="/status/"]'));
        if (statusLinks.some((link) => sourceStatusId(link.href) === wantedStatusId)) {
          return article;
        }
      }
    }
    return articles[0] ?? doc;
  }

  const sourceUrl = normalizedHttpUrl(sourcePostUrl ?? globalThis.location?.href);
  if (!sourceUrl || !isXStatusPage(sourceUrl)) return [];

  const root = postArticleRoot();
  if (!root) return [];

  const seenUrls = new Set<string>();
  const candidates: BrowserImageCandidate[] = [];
  const images = Array.from(root.querySelectorAll<HTMLImageElement>("img")).slice(0, maxImagesToInspect);
  for (const image of images) {
    const url = normalizedHttpUrl(image.currentSrc || image.src);
    if (!url || seenUrls.has(url)) continue;

    const width = renderedDimension(image, "width");
    const height = renderedDimension(image, "height");
    if (isLikelyExcludedImage(image, url, width, height)) continue;

    seenUrls.add(url);
    const alt = image.alt?.trim();
    candidates.push({
      url,
      source_post_url: sourceUrl,
      alt_text: alt ? alt.slice(0, maxAltTextLength) : undefined,
      width: width ?? undefined,
      height: height ?? undefined,
      role: imageRole(image) ?? undefined,
      order: candidates.length,
    });
    if (candidates.length >= maxCandidates) break;
  }

  return candidates;
}
