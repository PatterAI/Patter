"""Direction-aware, country-aware telephony pricing matrix.

The default Twilio telephony billing in ``pricing.py`` is a flat
$0.0085/min — the US **inbound local** rate. That number is correct for
the 99% case of an agent receiving calls on a US local number, but it
understates the true carrier cost for every other shape: US outbound
local ($0.014/min), US toll-free inbound ($0.022/min), and especially
international outbound (US → IT mobile is $0.3473/min — ~40x the
default). When the dashboard rolls up ``cost.telephony`` across a mixed
traffic mix, the under-estimation can swing total margin reporting by
10x or more.

This module exposes a per-country, per-direction, per-line-type rate
table sourced from Twilio's public per-country pricing pages
(``https://www.twilio.com/en-us/voice/pricing/<iso2>``), verified
2026-05-12, plus a stateless E.164 → ISO-2 country code parser. The
caller of :func:`getpatter.pricing.calculate_telephony_cost` opts in by
passing ``direction``, ``dest_country``, ``dest_type``; everything else
falls back to the legacy provider flat rate so existing integrations
bill identically to before this change.

Mobile-vs-landline detection from an E.164 number alone requires an
external HLR-lookup database we deliberately do not ship — picking
``"mobile"`` as the default ``dest_type`` makes the estimate
conservative (top-of-cost) and prevents systematic under-billing.
Operators with negotiated Twilio rates can override the entire matrix
via::

    Patter(pricing={"twilio_outbound_matrix": {...}})

Sources (all verified 2026-05-12, US-account perspective):
    - https://www.twilio.com/en-us/voice/pricing/us
    - https://www.twilio.com/en-us/voice/pricing/<iso2> (per destination)

Parity: keep this file in lockstep with
``libraries/typescript/src/services/telephony-pricing-matrix.ts``.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

logger = logging.getLogger("getpatter")

CallDirection = Literal["inbound", "outbound"]
DestLineType = Literal["landline", "mobile", "tollfree"]

# Twilio public per-country voice pricing (USD/min), US-account
# perspective. Verified 2026-05-12 against the public pricing pages.
# Operators with negotiated rates should override via
# ``Patter(pricing={"twilio_outbound_matrix": {...}})``.
TWILIO_PRICING_MATRIX: dict[str, dict] = {
    # United States — the legacy default ($0.0085/min) is inbound.local.
    "US": {
        "inbound": {"local": 0.0085, "tollfree": 0.022},
        "outbound": {"landline": 0.014, "mobile": 0.014, "tollfree": 0.014},
    },
    # Canada bundled with US under Twilio's "United States & Canada" rate.
    "CA": {
        "inbound": {"local": 0.0085},
        "outbound": {"landline": 0.014, "mobile": 0.014, "tollfree": 0.014},
    },
    # Italy — outbound mobile rate is dramatic: ~40x the US default.
    "IT": {
        "inbound": {"mobile": 0.01},
        "outbound": {"landline": 0.0168, "mobile": 0.3473},
    },
    # United Kingdom.
    "GB": {"outbound": {"landline": 0.0158, "mobile": 0.0305}},
    # Germany.
    "DE": {"outbound": {"landline": 0.021, "mobile": 0.042}},
    # France — non-EEA origin (US accounts pay this rate to FR mobile).
    "FR": {"outbound": {"landline": 0.0187, "mobile": 0.1603}},
    # Spain.
    "ES": {"outbound": {"landline": 0.0178, "mobile": 0.0388}},
    # Netherlands — non-EEA origin (US accounts hit the high rate).
    "NL": {"outbound": {"landline": 0.3675, "mobile": 0.2763}},
    # Brazil.
    "BR": {"outbound": {"landline": 0.031, "mobile": 0.0663}},
    # Mexico.
    "MX": {"outbound": {"landline": 0.016, "mobile": 0.0473}},
    # India.
    "IN": {"outbound": {"landline": 0.0497, "mobile": 0.0405}},
    # Japan.
    "JP": {"outbound": {"landline": 0.0746, "mobile": 0.185}},
    # Australia.
    "AU": {"outbound": {"landline": 0.0252, "mobile": 0.075}},
}

# Fallback outbound rate (USD/min) when the destination country is not
# present in :data:`TWILIO_PRICING_MATRIX`. Matches the legacy default
# exposed by ``DEFAULT_PRICING["twilio"]["price"]`` so existing
# integrations continue billing the same number when they don't pass a
# country.
TWILIO_DEFAULT_FALLBACK_RATE: float = 0.0085

# E.164 country-calling-code → ISO-2 country code lookup. Intentionally
# tiny: country-detection from a phone number is a hard problem at scale
# (NANPA shares +1 across 24 countries, +44 covers UK + Crown
# Dependencies) and a complete map would require an external dependency
# we don't ship. The leading ``+`` is stripped before lookup. Unknown
# numbers return ``None`` and the caller bills at the fallback rate.
COUNTRY_CODE_TO_ISO2: dict[str, str] = {
    "1": "US",  # Also covers CA, but matrix collapses both to the same rate.
    "7": "RU",
    "20": "EG",
    "27": "ZA",
    "30": "GR",
    "31": "NL",
    "32": "BE",
    "33": "FR",
    "34": "ES",
    "36": "HU",
    "39": "IT",
    "40": "RO",
    "41": "CH",
    "43": "AT",
    "44": "GB",
    "45": "DK",
    "46": "SE",
    "47": "NO",
    "48": "PL",
    "49": "DE",
    "51": "PE",
    "52": "MX",
    "53": "CU",
    "54": "AR",
    "55": "BR",
    "56": "CL",
    "57": "CO",
    "58": "VE",
    "60": "MY",
    "61": "AU",
    "62": "ID",
    "63": "PH",
    "64": "NZ",
    "65": "SG",
    "66": "TH",
    "81": "JP",
    "82": "KR",
    "84": "VN",
    "86": "CN",
    "90": "TR",
    "91": "IN",
    "92": "PK",
    "93": "AF",
    "94": "LK",
    "95": "MM",
    "98": "IR",
    "212": "MA",
    "213": "DZ",
    "216": "TN",
    "218": "LY",
    "220": "GM",
    "221": "SN",
    "234": "NG",
    "254": "KE",
    "255": "TZ",
    "256": "UG",
    "351": "PT",
    "352": "LU",
    "353": "IE",
    "354": "IS",
    "358": "FI",
    "420": "CZ",
    "421": "SK",
    "852": "HK",
    "853": "MO",
    "855": "KH",
    "856": "LA",
    "880": "BD",
    "886": "TW",
    "960": "MV",
    "961": "LB",
    "962": "JO",
    "963": "SY",
    "964": "IQ",
    "965": "KW",
    "966": "SA",
    "967": "YE",
    "968": "OM",
    "971": "AE",
    "972": "IL",
    "973": "BH",
    "974": "QA",
    "975": "BT",
    "976": "MN",
    "977": "NP",
}

_NON_DIGIT = re.compile(r"\D+")


def parse_e164_country(phone_number: str | None) -> str | None:
    """Parse an E.164 number into an ISO-2 country code, longest-prefix match.

    Returns ``None`` when the input is empty, malformed, or the country
    code is not in :data:`COUNTRY_CODE_TO_ISO2`. Pure / synchronous —
    no I/O.
    """
    if not phone_number:
        return None
    # Strip the leading + and any non-digit characters (spaces, dashes,
    # parentheses) that loose carriers occasionally include in To / From
    # headers. We do NOT validate length — invalid numbers fall through
    # to the no-match branch below.
    digits = _NON_DIGIT.sub("", phone_number)
    if not digits:
        return None
    # Longest-prefix match: try 3-digit prefix, then 2, then 1.
    for length in (3, 2, 1):
        if len(digits) < length:
            continue
        prefix = digits[:length]
        iso = COUNTRY_CODE_TO_ISO2.get(prefix)
        if iso:
            return iso
    return None


def resolve_twilio_rate(
    direction: CallDirection | None,
    dest_country: str | None,
    dest_type: DestLineType = "mobile",
    override_matrix: dict[str, dict] | None = None,
) -> float:
    """Resolve the Twilio per-minute rate (USD) for a single call segment.

    Args:
        direction: ``"inbound"`` (agent received) or ``"outbound"``
            (agent placed). When ``None``, falls back to
            :data:`TWILIO_DEFAULT_FALLBACK_RATE`.
        dest_country: ISO-2 country code of the remote party. When
            ``None`` or unknown, falls back to
            :data:`TWILIO_DEFAULT_FALLBACK_RATE`.
        dest_type: ``"mobile"`` is the conservative default for unknown
            line types — international mobile rates are universally
            higher than landline, and under-billing is worse than
            over-billing on a margin-reporting dashboard.
        override_matrix: Optional user-supplied override matrix that
            wins over :data:`TWILIO_PRICING_MATRIX`. Lets operators
            inject negotiated carrier rates without forking the SDK.
    """
    if not direction or not dest_country:
        return TWILIO_DEFAULT_FALLBACK_RATE
    matrix = override_matrix if override_matrix is not None else TWILIO_PRICING_MATRIX
    country = matrix.get(dest_country.upper())
    if not country:
        logger.debug(
            "telephony pricing: unknown destination country %r, "
            "billing at fallback rate $%.4f/min",
            dest_country,
            TWILIO_DEFAULT_FALLBACK_RATE,
        )
        return TWILIO_DEFAULT_FALLBACK_RATE
    if direction == "inbound":
        inbound = country.get("inbound")
        if not inbound:
            return TWILIO_DEFAULT_FALLBACK_RATE
        if dest_type == "tollfree" and "tollfree" in inbound:
            return inbound["tollfree"]
        if dest_type == "mobile" and "mobile" in inbound:
            return inbound["mobile"]
        if "local" in inbound:
            return inbound["local"]
        return TWILIO_DEFAULT_FALLBACK_RATE
    # Outbound.
    outbound = country.get("outbound") or {}
    if dest_type == "tollfree" and "tollfree" in outbound:
        return outbound["tollfree"]
    if dest_type == "landline" and "landline" in outbound:
        return outbound["landline"]
    # Default to mobile when dest_type is None or "mobile".
    return outbound.get("mobile", TWILIO_DEFAULT_FALLBACK_RATE)
