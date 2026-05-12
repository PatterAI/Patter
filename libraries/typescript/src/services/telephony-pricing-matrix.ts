/**
 * Direction-aware, country-aware telephony pricing matrix.
 *
 * Default Twilio telephony billing in ``pricing.ts`` is a flat $0.0085/min
 * — the US **inbound local** rate. That number is correct for the 99% case
 * of an agent receiving calls on a US local number, but it understates the
 * true carrier cost for every other shape: US outbound local ($0.014/min),
 * US toll-free inbound ($0.022/min), and especially international outbound
 * (US → IT mobile is $0.3473/min — ~40x the default). When the dashboard
 * rolls up ``cost.telephony`` across a mixed traffic mix, the under-
 * estimation can swing total margin reporting by 10x or more.
 *
 * This module exposes a per-country, per-direction, per-line-type rate
 * table sourced from Twilio's public per-country pricing pages
 * (``https://www.twilio.com/en-us/voice/pricing/<iso2>``), verified
 * 2026-05-12, plus a stateless E.164 → ISO-2 country code parser. The
 * caller of ``calculateTelephonyCost`` opts in by passing
 * ``{ direction, destCountry, destType }``; everything else falls back to
 * the legacy provider flat rate so existing integrations bill identically
 * to before this change.
 *
 * Mobile-vs-landline detection from an E.164 number alone requires an
 * external HLR-lookup database we deliberately do not ship — picking
 * ``"mobile"`` as the default ``destType`` makes the estimate conservative
 * (top-of-cost) and prevents systematic under-billing. Operators with
 * negotiated Twilio rates can override the entire matrix via::
 *
 *     new Patter({ pricing: { twilio_outbound_matrix: {...} } })
 *
 * Sources (all verified 2026-05-12, US-account perspective):
 *   - https://www.twilio.com/en-us/voice/pricing/us
 *   - https://www.twilio.com/en-us/voice/pricing/<iso2> (per destination)
 *
 * Parity: keep this file in lockstep with
 * ``libraries/python/getpatter/services/telephony_pricing_matrix.py``.
 */

/** Direction of a single phone call relative to the agent. */
export type CallDirection = 'inbound' | 'outbound';

/** Line-type bucket used by Twilio's per-country pricing pages. */
export type DestLineType = 'landline' | 'mobile' | 'tollfree';

/** Per-country outbound rates in USD per minute. */
export interface OutboundRates {
  readonly landline: number;
  readonly mobile: number;
  readonly tollfree?: number;
}

/** Per-country inbound rates in USD per minute. */
export interface InboundRates {
  readonly local?: number;
  readonly tollfree?: number;
  readonly mobile?: number;
}

/** Combined inbound + outbound entry keyed by ISO 3166-1 alpha-2 country code. */
export interface CountryPricing {
  readonly inbound?: InboundRates;
  readonly outbound: OutboundRates;
}

/**
 * Twilio public per-country voice pricing (USD/min), US-account perspective.
 *
 * Verified 2026-05-12 against the public pricing pages. Operators with
 * negotiated rates should override via
 * ``new Patter({ pricing: { twilio_outbound_matrix: {...} } })``.
 */
export const TWILIO_PRICING_MATRIX: Readonly<Record<string, CountryPricing>> = {
  // United States — the legacy default ($0.0085/min) is inbound.local.
  US: {
    inbound: { local: 0.0085, tollfree: 0.022 },
    outbound: { landline: 0.014, mobile: 0.014, tollfree: 0.014 },
  },
  // Canada bundled with US under Twilio's "United States & Canada" rate.
  CA: {
    inbound: { local: 0.0085 },
    outbound: { landline: 0.014, mobile: 0.014, tollfree: 0.014 },
  },
  // Italy — outbound mobile rate is dramatic: 40x the US default.
  IT: {
    inbound: { mobile: 0.01 },
    outbound: { landline: 0.0168, mobile: 0.3473 },
  },
  // United Kingdom.
  GB: {
    outbound: { landline: 0.0158, mobile: 0.0305 },
  },
  // Germany.
  DE: {
    outbound: { landline: 0.021, mobile: 0.042 },
  },
  // France — non-EEA origin (US accounts pay this rate to FR mobile).
  FR: {
    outbound: { landline: 0.0187, mobile: 0.1603 },
  },
  // Spain.
  ES: {
    outbound: { landline: 0.0178, mobile: 0.0388 },
  },
  // Netherlands — non-EEA origin (US accounts hit the high rate).
  NL: {
    outbound: { landline: 0.3675, mobile: 0.2763 },
  },
  // Brazil.
  BR: {
    outbound: { landline: 0.031, mobile: 0.0663 },
  },
  // Mexico.
  MX: {
    outbound: { landline: 0.016, mobile: 0.0473 },
  },
  // India.
  IN: {
    outbound: { landline: 0.0497, mobile: 0.0405 },
  },
  // Japan.
  JP: {
    outbound: { landline: 0.0746, mobile: 0.185 },
  },
  // Australia.
  AU: {
    outbound: { landline: 0.0252, mobile: 0.075 },
  },
};

/**
 * Fallback outbound rate (USD/min) when the destination country is not
 * present in :data:`TWILIO_PRICING_MATRIX`. Matches the legacy default
 * exposed by ``DEFAULT_PRICING.twilio.price`` so existing integrations
 * continue billing the same number when they don't pass a country.
 */
export const TWILIO_DEFAULT_FALLBACK_RATE = 0.0085;

/**
 * E.164 country-calling-code → ISO-2 country code lookup.
 *
 * Covers the destinations in :data:`TWILIO_PRICING_MATRIX` plus the most
 * common +-prefix shapes a deployed Patter caller might see. Intentionally
 * tiny: country-detection from a phone number is a hard problem at scale
 * (NANPA shares +1 across 24 countries, +44 covers UK + Crown Dependencies)
 * and a complete map would require an external dependency we don't ship.
 *
 * The leading ``+`` is stripped before lookup. Longest-prefix match wins
 * so the three-digit ``+351`` (Portugal) resolves before any one-digit
 * ambiguity is considered. Unknown numbers return ``null`` and the caller
 * bills at the fallback rate.
 */
export const COUNTRY_CODE_TO_ISO2: Readonly<Record<string, string>> = {
  '1': 'US', // Also covers CA, but matrix collapses both to the same rate.
  '7': 'RU',
  '20': 'EG',
  '27': 'ZA',
  '30': 'GR',
  '31': 'NL',
  '32': 'BE',
  '33': 'FR',
  '34': 'ES',
  '36': 'HU',
  '39': 'IT',
  '40': 'RO',
  '41': 'CH',
  '43': 'AT',
  '44': 'GB',
  '45': 'DK',
  '46': 'SE',
  '47': 'NO',
  '48': 'PL',
  '49': 'DE',
  '51': 'PE',
  '52': 'MX',
  '53': 'CU',
  '54': 'AR',
  '55': 'BR',
  '56': 'CL',
  '57': 'CO',
  '58': 'VE',
  '60': 'MY',
  '61': 'AU',
  '62': 'ID',
  '63': 'PH',
  '64': 'NZ',
  '65': 'SG',
  '66': 'TH',
  '81': 'JP',
  '82': 'KR',
  '84': 'VN',
  '86': 'CN',
  '90': 'TR',
  '91': 'IN',
  '92': 'PK',
  '93': 'AF',
  '94': 'LK',
  '95': 'MM',
  '98': 'IR',
  '212': 'MA',
  '213': 'DZ',
  '216': 'TN',
  '218': 'LY',
  '220': 'GM',
  '221': 'SN',
  '234': 'NG',
  '254': 'KE',
  '255': 'TZ',
  '256': 'UG',
  '351': 'PT',
  '352': 'LU',
  '353': 'IE',
  '354': 'IS',
  '358': 'FI',
  '420': 'CZ',
  '421': 'SK',
  '852': 'HK',
  '853': 'MO',
  '855': 'KH',
  '856': 'LA',
  '880': 'BD',
  '886': 'TW',
  '960': 'MV',
  '961': 'LB',
  '962': 'JO',
  '963': 'SY',
  '964': 'IQ',
  '965': 'KW',
  '966': 'SA',
  '967': 'YE',
  '968': 'OM',
  '971': 'AE',
  '972': 'IL',
  '973': 'BH',
  '974': 'QA',
  '975': 'BT',
  '976': 'MN',
  '977': 'NP',
};

/**
 * Parse an E.164 number into an ISO-2 country code, longest-prefix match.
 *
 * Returns ``null`` when the input is empty, malformed, or the country code
 * is not in :data:`COUNTRY_CODE_TO_ISO2`. Pure / synchronous — no I/O.
 */
export function parseE164Country(phoneNumber: string | undefined | null): string | null {
  if (!phoneNumber) return null;
  // Strip the leading + and any non-digit characters (spaces, dashes,
  // parentheses) that loose carriers occasionally include in the To / From
  // headers. We do NOT validate length — invalid numbers fall through to
  // the no-match branch below.
  const digits = phoneNumber.replace(/[^\d]/g, '');
  if (!digits) return null;
  // Longest-prefix match: try 3-digit prefix, then 2, then 1.
  for (let len = 3; len >= 1; len -= 1) {
    if (digits.length < len) continue;
    const prefix = digits.slice(0, len);
    const iso = COUNTRY_CODE_TO_ISO2[prefix];
    if (iso) return iso;
  }
  return null;
}

/**
 * Resolve the Twilio per-minute rate (USD) for a single call segment.
 *
 * @param direction - ``"inbound"`` (agent received) or ``"outbound"``
 *   (agent placed). When ``undefined``, falls back to
 *   :data:`TWILIO_DEFAULT_FALLBACK_RATE`.
 * @param destCountry - ISO-2 country code of the remote party. When
 *   ``undefined`` or unknown, falls back to
 *   :data:`TWILIO_DEFAULT_FALLBACK_RATE`.
 * @param destType - ``"mobile"`` is the conservative default for
 *   unknown line types because international mobile rates are
 *   universally higher than landline — under-billing is worse than
 *   over-billing on a margin-reporting dashboard.
 * @param overrideMatrix - Optional user-supplied override matrix that
 *   wins over :data:`TWILIO_PRICING_MATRIX`. Lets operators inject
 *   negotiated carrier rates without forking the SDK.
 */
export function resolveTwilioRate(
  direction: CallDirection | undefined,
  destCountry: string | undefined | null,
  destType: DestLineType = 'mobile',
  overrideMatrix?: Readonly<Record<string, CountryPricing>>,
): number {
  if (!direction || !destCountry) return TWILIO_DEFAULT_FALLBACK_RATE;
  const matrix = overrideMatrix ?? TWILIO_PRICING_MATRIX;
  const country = matrix[destCountry.toUpperCase()];
  if (!country) return TWILIO_DEFAULT_FALLBACK_RATE;
  if (direction === 'inbound') {
    const inbound = country.inbound;
    if (!inbound) return TWILIO_DEFAULT_FALLBACK_RATE;
    if (destType === 'tollfree' && inbound.tollfree !== undefined) return inbound.tollfree;
    if (destType === 'mobile' && inbound.mobile !== undefined) return inbound.mobile;
    if (inbound.local !== undefined) return inbound.local;
    return TWILIO_DEFAULT_FALLBACK_RATE;
  }
  // Outbound.
  const outbound = country.outbound;
  if (destType === 'tollfree' && outbound.tollfree !== undefined) return outbound.tollfree;
  if (destType === 'landline') return outbound.landline;
  // Default to mobile when destType is undefined or "mobile".
  return outbound.mobile;
}
