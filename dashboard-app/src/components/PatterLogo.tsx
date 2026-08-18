import type { SVGProps } from 'react';

// Callable mark — the Silver & Signal system's 24x24 squircle, signal-green
// fill carrying one continuous white stroke that starts as three waves and
// flattens into a straight line (a voice becoming a connected call). Fixed
// brand colours, not currentColor: the mark reads identically in light and
// dark mode, matching how the design system renders it everywhere else
// (source: gtm/brand/callable-design-system/assets/logo.svg).
function CallableMark(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      role="img"
      aria-hidden="true"
      {...props}
    >
      <rect width="24" height="24" rx="7" fill="#00873F" />
      <path
        d="M3.5 12 C4.6 6.8 6.4 6.8 7.5 12 S10 16.4 11.5 12 S14.2 9.6 15.5 12 S18.5 12 20.5 12"
        stroke="#FFFFFF"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// Composite — mark on the left, "Patter" set live in Host Grotesk on the
// right (this dashboard is SDK-specific chrome, so the wordmark stays
// "Patter" rather than the product-level "Callable" wordmark). The text
// uses ``currentColor`` so it inherits the parent ``.brand`` colour — which
// tokens.css's ``body.dark`` block already flips to ``--frost`` — with no
// separate dark asset needed. The mark's own fill/stroke are fixed (see
// above) and intentionally do not follow ``currentColor``.
export function PatterLogo() {
  return (
    <span
      className="patter-logo"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
      }}
      aria-label="Patter"
    >
      <CallableMark width={26} height={26} />
      <span
        style={{
          fontFamily: 'var(--font-display)',
          fontWeight: 600,
          fontSize: 19,
          letterSpacing: '-0.04em',
          color: 'currentColor',
        }}
      >
        Patter
      </span>
    </span>
  );
}
