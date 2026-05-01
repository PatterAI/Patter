"""§2 Feature Tour cells — 07 Telephony Telnyx."""

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
            "Exercises Telnyx carrier construction and Ed25519 webhook signature verification.\n"
        ),
        _md("### Telnyx carrier construction\n"),
        _code(
            "ft_telnyx_carrier",
            "from getpatter import Patter, Telnyx\n"
            "with _setup.cell('telnyx_carrier', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        carrier = Telnyx(\n"
            "            api_key='KEY0_TEST_TELNYX',\n"
            "            public_key='',  # Ed25519 public key (omitted for offline test)\n"
            "        )\n"
            "        p = Patter(\n"
            "            carrier=carrier,\n"
            "            phone_number='+15555550100',\n"
            "            webhook_url='https://example.com/webhook',\n"
            "        )\n"
            "        lc = p._local_config\n"
            "        print(f'carrier:  {lc.telephony_provider}')\n"
            "        print(f'phone:    {lc.phone_number}')\n"
            "        assert lc.telephony_provider == 'telnyx'\n",
        ),
        _md("### Ed25519 sign + verify\n"),
        _code(
            "ft_ed25519_verify",
            "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey\n"
            "from cryptography.hazmat.primitives.serialization import (\n"
            "    Encoding, PrivateFormat, PublicFormat, NoEncryption,\n"
            "    load_pem_private_key, load_pem_public_key,\n"
            ")\n"
            "with _setup.cell('ed25519_verify', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        # Load test keypair from fixtures.\n"
            "        priv_pem = _setup.load_fixture('keys/telnyx_test_ed25519_private.pem')\n"
            "        pub_pem  = _setup.load_fixture('keys/telnyx_test_ed25519_public.pem')\n"
            "        private_key = load_pem_private_key(priv_pem, password=None)\n"
            "        public_key  = load_pem_public_key(pub_pem)\n"
            "        payload = b'telnyx-webhook-payload'\n"
            "        signature = private_key.sign(payload)\n"
            "        public_key.verify(signature, payload)  # raises if invalid\n"
            "        print(f'Ed25519 sign + verify OK  sig_len={len(signature)} bytes')\n"
            "        # Tampered payload must fail.\n"
            "        import pytest\n"
            "        from cryptography.exceptions import InvalidSignature\n"
            "        try:\n"
            "            public_key.verify(signature, b'tampered-payload')\n"
            "            print('ERROR: tampered payload should have raised InvalidSignature')\n"
            "        except InvalidSignature:\n"
            "            print('Tampered payload correctly rejected')\n",
        ),
        _md("### Telnyx anti-replay: timestamp window check\n"),
        _code(
            "ft_telnyx_timestamp",
            "import time\n"
            "with _setup.cell('telnyx_timestamp', tier=1, env=env) as ok:\n"
            "    if ok:\n"
            "        WINDOW_SECONDS = 300  # ±5 minutes\n"
            "        now = int(time.time())\n"
            "        cases = [\n"
            "            (now,              'fresh   — should pass'),\n"
            "            (now - 60,         'recent  — should pass'),\n"
            "            (now - 299,        'edge    — should pass'),\n"
            "            (now - 301,        'stale   — should reject'),\n"
            "            (now + 10,         'future  — should pass'),\n"
            "        ]\n"
            "        for ts, label in cases:\n"
            "            age = abs(now - ts)\n"
            "            accepted = age <= WINDOW_SECONDS\n"
            "            mark = '✓' if accepted else '✗'\n"
            "            print(f'  {mark} age={age:3d}s  {label}')\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md(
            "## §2 — Feature Tour\n\n"
            "Exercises Telnyx carrier construction and Ed25519 webhook signature verification.\n"
        ),
        _md("### Telnyx carrier construction\n"),
        _code(
            "ft_telnyx_carrier",
            'import { Patter, Telnyx } from "getpatter";\n'
            "await cell('telnyx_carrier', { tier: 1, env }, () => {\n"
            "  const p = new Patter({\n"
            "    carrier: new Telnyx({ apiKey: 'KEY0_TEST_TELNYX', publicKey: '' }),\n"
            "    phoneNumber: '+15555550100',\n"
            "    webhookUrl: 'https://example.com/webhook',\n"
            "  });\n"
            "  console.log('Telnyx carrier constructed OK');\n"
            "});\n",
        ),
        _md("### Ed25519 sign + verify\n"),
        _code(
            "ft_ed25519_verify",
            "import { webcrypto } from 'crypto';\n"
            "await cell('ed25519_verify', { tier: 1, env }, async () => {\n"
            "  const { subtle } = webcrypto;\n"
            "  const keypair = await subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);\n"
            "  const payload = new TextEncoder().encode('telnyx-webhook-payload');\n"
            "  const signature = await subtle.sign('Ed25519', keypair.privateKey, payload);\n"
            "  const valid = await subtle.verify('Ed25519', keypair.publicKey, signature, payload);\n"
            "  console.log(`Ed25519 sign+verify: ${valid}  sig_len=${signature.byteLength}`);\n"
            "  const tampered = new TextEncoder().encode('tampered');\n"
            "  const invalidOk = !(await subtle.verify('Ed25519', keypair.publicKey, signature, tampered));\n"
            "  console.log(`Tampered payload rejected: ${invalidOk}`);\n"
            "});\n",
        ),
        _md("### Telnyx anti-replay: timestamp window check\n"),
        _code(
            "ft_telnyx_timestamp",
            "await cell('telnyx_timestamp', { tier: 1, env }, () => {\n"
            "  const WINDOW = 300;\n"
            "  const now = Math.floor(Date.now() / 1000);\n"
            "  const cases: [number, string][] = [\n"
            "    [now,       'fresh'],\n"
            "    [now - 60,  'recent'],\n"
            "    [now - 301, 'stale — reject'],\n"
            "  ];\n"
            "  for (const [ts, label] of cases) {\n"
            "    const age = Math.abs(now - ts);\n"
            "    const ok = age <= WINDOW ? '✓' : '✗';\n"
            "    console.log(`  ${ok} age=${age}s  ${label}`);\n"
            "  }\n"
            "});\n",
        ),
    ]
