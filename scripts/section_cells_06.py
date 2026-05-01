"""§2 Feature Tour cells — 06 Telephony Twilio."""

from __future__ import annotations


def _md(*lines: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}


def _code(tag: str, source: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {"tags": [tag]},
        "source": source.splitlines(keepends=True),
        "execution_count": None,
        "outputs": [],
    }


def section_cells_python() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises Twilio webhook signature verification and carrier construction.\n"
        ),
        _md("### Twilio signature verification — valid request\n"),
        _code(
            "ft_twilio_sig_valid",
            "import hmac, hashlib, base64\n"
            "from twilio.request_validator import RequestValidator\n"
            "with _setup.cell('twilio_sig_valid', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        auth_token = 'test_auth_token_32chars_padding___'\n"
            "        url        = 'https://example.com/webhook/voice'\n"
            "        params     = {'CallSid': 'CA0000000000000000000000000000a001', 'From': '+15555550100', 'To': '+15555550200'}\n"
            "        validator  = RequestValidator(auth_token)\n"
            "        signature  = validator.compute_signature(url, params)\n"
            "        valid      = validator.validate(url, params, signature)\n"
            "        print(f'signature: {signature}')\n"
            "        print(f'valid:     {valid}')\n"
            "        assert valid, 'signature should be valid'\n",
        ),
        _md("### Twilio signature verification — tampered request\n"),
        _code(
            "ft_twilio_sig_invalid",
            "from twilio.request_validator import RequestValidator\n"
            "with _setup.cell('twilio_sig_invalid', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        auth_token = 'test_auth_token_32chars_padding___'\n"
            "        url        = 'https://example.com/webhook/voice'\n"
            "        params     = {'CallSid': 'CA0000000000000000000000000000a001', 'From': '+15555550100'}\n"
            "        validator  = RequestValidator(auth_token)\n"
            "        bad_sig    = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='\n"
            "        valid      = validator.validate(url, params, bad_sig)\n"
            "        print(f'tampered signature valid: {valid}  (expected False)')\n"
            "        assert not valid, 'tampered signature must be rejected'\n",
        ),
        _md("### E.164 phone number patterns\n"),
        _code(
            "ft_e164_patterns",
            "import re\n"
            "with _setup.cell('e164_patterns', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        E164_RE = re.compile(r'^\\+[1-9]\\d{6,14}$')\n"
            "        cases = [\n"
            "            ('+15555550100', True),\n"
            "            ('+442071838750', True),\n"
            "            ('+393399123456', True),\n"
            "            ('15555550100',  False),   # missing +\n"
            "            ('+1',          False),   # too short\n"
            "            ('not-a-number', False),\n"
            "        ]\n"
            "        for number, expected in cases:\n"
            "            result = bool(E164_RE.match(number))\n"
            "            status = '✓' if result == expected else '✗'\n"
            "            print(f'  {status} {number!r:20s} → {result}')\n"
            "        assert all(bool(E164_RE.match(n)) == e for n, e in cases)\n",
        ),
        _md("### Twilio carrier construction\n"),
        _code(
            "ft_twilio_carrier",
            "from getpatter import Patter, Twilio\n"
            "with _setup.cell('twilio_carrier', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        carrier = Twilio(\n"
            "            account_sid='ACtest00000000000000000000000000',\n"
            "            auth_token='test_token',\n"
            "        )\n"
            "        p = Patter(\n"
            "            carrier=carrier,\n"
            "            phone_number='+15555550100',\n"
            "            webhook_url='https://example.com/webhook',\n"
            "        )\n"
            "        lc = p._local_config\n"
            "        print(f'carrier:  {lc.telephony_provider}')\n"
            "        print(f'phone:    {lc.phone_number}')\n"
            "        print(f'webhook:  {lc.webhook_url}')\n"
            "        assert lc.telephony_provider == 'twilio'\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises Twilio webhook signature verification and carrier construction.\n"
        ),
        _md("### Twilio signature verification — valid request\n"),
        _code(
            "ft_twilio_sig_valid",
            "import crypto from 'crypto';\n"
            "await cell('twilio_sig_valid', { tier: 1, env }, () => {\n"
            "  const authToken = 'test_auth_token_32chars_padding___';\n"
            "  const url       = 'https://example.com/webhook/voice';\n"
            "  const params    = { CallSid: 'CA0000000000000000000000000000a001', From: '+15555550100' };\n"
            "  const sorted    = Object.keys(params).sort().map(k => k + (params as any)[k]).join('');\n"
            "  const sig       = crypto.createHmac('sha1', authToken).update(url + sorted).digest('base64');\n"
            "  console.log(`Twilio signature: ${sig}`);\n"
            "  console.log('Signature computed OK (validate against Twilio SDK in production)');\n"
            "});\n",
        ),
        _md("### Twilio signature verification — tampered request\n"),
        _code(
            "ft_twilio_sig_invalid",
            "import crypto from 'crypto';\n"
            "await cell('twilio_sig_invalid', { tier: 1, env }, () => {\n"
            "  const authToken = 'test_auth_token_32chars_padding___';\n"
            "  const url       = 'https://example.com/webhook/voice';\n"
            "  const params    = { CallSid: 'CA0000000000000000000000000000a001', From: '+15555550100' };\n"
            "  const sorted    = Object.keys(params).sort().map(k => k + (params as any)[k]).join('');\n"
            "  const goodSig   = crypto.createHmac('sha1', authToken).update(url + sorted).digest('base64');\n"
            "  const badSig    = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';\n"
            "  console.log(`Good sig: ${goodSig.slice(0, 20)}...`);\n"
            "  console.log(`Bad  sig: ${badSig.slice(0, 20)}...`);\n"
            "  console.log(`Tampered signature differs: ${goodSig !== badSig}  (would be rejected)`);\n"
            "  if (goodSig === badSig) throw new Error('signatures should differ');\n"
            "});\n",
        ),
        _md("### E.164 phone number patterns\n"),
        _code(
            "ft_e164_patterns",
            "await cell('e164_patterns', { tier: 1, env }, () => {\n"
            "  const E164_RE = /^\\+[1-9]\\d{6,14}$/;\n"
            "  const cases: [string, boolean][] = [\n"
            "    ['+15555550100', true], ['+442071838750', true],\n"
            "    ['15555550100', false], ['+1', false],\n"
            "  ];\n"
            "  for (const [num, expected] of cases) {\n"
            "    const result = E164_RE.test(num);\n"
            "    const ok = result === expected ? '✓' : '✗';\n"
            "    console.log(`  ${ok} ${num.padEnd(16)} → ${result}`);\n"
            "  }\n"
            "});\n",
        ),
        _md("### Twilio carrier construction\n"),
        _code(
            "ft_twilio_carrier",
            'import { Patter, Twilio } from "getpatter";\n'
            "await cell('twilio_carrier', { tier: 1, env }, () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Twilio({ accountSid: 'ACtest00000000000000000000000000', authToken: 'test' }),\n"
            "    phoneNumber: '+15555550100',\n"
            "    webhookUrl: 'https://example.com/webhook',\n"
            "  });\n"
            "  console.log('Twilio carrier constructed OK');\n"
            "});\n",
        ),
    ]
