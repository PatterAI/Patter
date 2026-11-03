import type { SVGProps } from 'react';

// Callable mark — the Silver & Signal system's 24x24 squircle, signal-green
// fill with a white "C" glyph (the live Callable mark from callable.studio).
// Fixed brand colours, not currentColor: the mark reads identically in light
// and dark mode, matching how the design system renders it everywhere else
// (source: gtm/marketing/channels/x/live/profile/mark-live.svg).
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
      <g
        transform="translate(7.085,5.250) scale(0.019941) translate(-385.1,1014.7) scale(0.1,-0.1)"
        fill="#FFFFFF"
      >
        <path d="M6429 10134 c-655 -75 -1266 -298 -1736 -631 -111 -79 -295 -245 -396 -360 -193 -217 -342 -497 -418 -788 -22 -83 -24 -109 -24 -330 0 -220 2 -248 24 -330 50 -195 132 -379 238 -537 61 -92 192 -258 203 -258 4 0 39 64 78 143 216 436 543 765 1003 1006 241 127 475 209 754 266 240 49 355 54 1195 56 399 0 863 4 1032 7 l307 7 30 29 c17 16 35 43 40 60 6 18 13 353 17 786 7 747 6 756 -14 795 -11 22 -32 51 -47 65 l-27 25 -1071 1 c-841 2 -1097 -1 -1188 -12z" />
        <path d="M3853 5990 c5 -685 6 -743 26 -852 106 -599 513 -1104 1146 -1420 283 -142 565 -228 958 -294 l152 -26 1249 -10 1248 -11 43 21 c24 11 55 36 69 54 l25 34 3 785 c3 866 5 846 -58 894 l-35 27 -1197 8 -1197 8 -160 26 c-362 59 -638 147 -953 305 -343 171 -566 334 -853 620 -186 187 -249 261 -374 436 -43 61 -82 115 -88 120 -6 6 -7 -262 -4 -725z" />
      </g>
    </svg>
  );
}

// Composite — mark on the left, "Patter" set live in Host Grotesk on the
// right (this dashboard is SDK-specific chrome, so the wordmark stays
// "Patter" rather than the product-level "Callable" wordmark). The text
// uses ``currentColor`` so it inherits the parent ``.brand`` colour — which
// tokens.css's ``body.dark`` block already flips to ``--frost`` — with no
// separate dark asset needed. The mark's own fill is fixed (see above) and
// intentionally does not follow ``currentColor``.
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
