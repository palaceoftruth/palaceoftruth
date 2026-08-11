import type { AnchorHTMLAttributes, ReactNode } from "react";

import { safeExternalUrl } from "../lib/safeUrl";

type SafeExternalLinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & {
  href: unknown;
  children: ReactNode;
};

/**
 * Anchor for server-supplied URLs. The scheme allow-list runs at the sink, so a
 * `javascript:` URL that ever got past backend validation renders as inert text
 * instead of executing on click.
 */
export default function SafeExternalLink({
  href,
  children,
  target = "_blank",
  rel = "noopener noreferrer",
  ...rest
}: SafeExternalLinkProps) {
  const safeHref = safeExternalUrl(href);
  if (!safeHref) {
    // Keep the content and styling; drop only the navigation.
    const { className, title } = rest;
    return (
      <span className={className} title={title}>
        {children}
      </span>
    );
  }
  return (
    <a href={safeHref} target={target} rel={rel} {...rest}>
      {children}
    </a>
  );
}
