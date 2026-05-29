from dataclasses import dataclass


@dataclass(frozen=True)
class LocalConfig:
    telephony_provider: str = "twilio"
    twilio_sid: str = ""
    twilio_token: str = ""
    telnyx_key: str = ""
    telnyx_connection_id: str = ""
    telnyx_public_key: str = ""
    plivo_auth_id: str = ""
    plivo_auth_token: str = ""
    openai_key: str = ""
    elevenlabs_key: str = ""
    deepgram_key: str = ""
    cartesia_key: str = ""
    rime_key: str = ""
    lmnt_key: str = ""
    soniox_key: str = ""
    speechmatics_key: str = ""
    assemblyai_key: str = ""
    phone_number: str = ""
    webhook_url: str = ""
    # SECURITY: require valid webhook signatures on Twilio, Telnyx and Plivo
    # inbound webhooks. When True (the default), a missing credential
    # (twilio auth token / telnyx public key / plivo auth token) causes the
    # webhook to return 503 Service Unavailable instead of silently
    # accepting the request.
    # Set to False only for local development against mock providers.
    require_signature: bool = True
    # When True, only the very first TTFB event per turn is emitted to the
    # EventBus. Default is False to preserve current per-segment emission
    # behaviour.
    report_only_initial_ttfb: bool = False
    # Resolved on-disk persistence root for the dashboard's call history,
    # or ``None`` to disable. Computed by ``client.py`` from the public
    # ``Patter(persist=...)`` option (with ``PATTER_LOG_DIR`` env-var
    # fallback). When ``None``, ``CallLogger`` is a no-op and the dashboard
    # is in-memory-only — restarts wipe history.
    persist_root: str | None = None
