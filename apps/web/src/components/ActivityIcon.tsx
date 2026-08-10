/** Super-simple category glyphs for calendar cards (monoline, currentColor). */

import type { ReactNode } from "react";

type Props = {
  kind: string;
  className?: string;
};

const view = "0 0 24 24";

function Svg({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <svg
      className={className}
      viewBox={view}
      width="24"
      height="24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {children}
    </svg>
  );
}

export function ActivityIcon({ kind, className }: Props) {
  switch (kind) {
    case "sports":
      return (
        <Svg className={className}>
          <circle cx="12" cy="12" r="9" />
          <path d="M12 3v18M3 12h18" />
          <path d="M5.5 6.5c2.2 1.6 4.4 2.2 6.5 2.2s4.3-.6 6.5-2.2" />
          <path d="M5.5 17.5c2.2-1.6 4.4-2.2 6.5-2.2s4.3.6 6.5 2.2" />
        </Svg>
      );
    case "school":
      return (
        <Svg className={className}>
          <path d="M3 10.5 12 5l9 5.5-9 5.5-9-5.5Z" />
          <path d="M7 13v4.5c0 .8 2.2 2.5 5 2.5s5-1.7 5-2.5V13" />
          <path d="M21 10.5V16" />
        </Svg>
      );
    case "work":
      return (
        <Svg className={className}>
          <rect x="3" y="8" width="18" height="12" rx="2" />
          <path d="M8 8V6.5A2.5 2.5 0 0 1 10.5 4h3A2.5 2.5 0 0 1 16 6.5V8" />
          <path d="M3 13h18" />
        </Svg>
      );
    case "medical":
      return (
        <Svg className={className}>
          <path d="M9 3h6v5h5v6h-5v7H9v-7H4V8h5V3Z" />
        </Svg>
      );
    case "family":
      return (
        <Svg className={className}>
          <circle cx="8" cy="7" r="2.5" />
          <circle cx="16" cy="8" r="2" />
          <path d="M3.5 19c.4-3.2 2.4-5 4.5-5s4.1 1.8 4.5 5" />
          <path d="M13 19c.3-2.4 1.7-3.8 3-3.8s2.7 1.4 3 3.8" />
        </Svg>
      );
    case "food":
      return (
        <Svg className={className}>
          <path d="M7 3v8" />
          <path d="M5 3v4a2 2 0 0 0 4 0V3" />
          <path d="M7 11v10" />
          <path d="M16 3v7a2.5 2.5 0 0 0 2.5 2.5H19" />
          <path d="M16.5 12.5V21" />
        </Svg>
      );
    case "home":
      return (
        <Svg className={className}>
          <path d="M4 11.5 12 4l8 7.5" />
          <path d="M6.5 10.5V20h11v-9.5" />
          <path d="M10 20v-5h4v5" />
        </Svg>
      );
    case "meeting":
      return (
        <Svg className={className}>
          <rect x="3" y="5" width="18" height="12" rx="2" />
          <path d="M8 21h8" />
          <path d="M12 17v4" />
          <circle cx="9" cy="11" r="1.2" fill="currentColor" stroke="none" />
          <circle cx="12" cy="11" r="1.2" fill="currentColor" stroke="none" />
          <circle cx="15" cy="11" r="1.2" fill="currentColor" stroke="none" />
        </Svg>
      );
    case "travel":
      return (
        <Svg className={className}>
          <path d="M10 4 3.5 9.5h4L6 20h3l2-6 2 6h3l-1.5-10.5h4L14 4h-4Z" />
        </Svg>
      );
    default:
      return (
        <Svg className={className}>
          <rect x="4" y="5" width="16" height="15" rx="2" />
          <path d="M8 3v4M16 3v4M4 11h16" />
        </Svg>
      );
  }
}
