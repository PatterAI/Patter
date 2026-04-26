from dataclasses import dataclass


@dataclass(frozen=True)
class LocalConfig:
    telephony_provider: str = "twilio"
    twilio_sid: str = ""
    twilio_token: str = ""
    telnyx_key: str = ""
    telnyx_connection_id: str = ""
    telnyx_public_key: str = ""
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
    # SECURITY: require valid webhook signatures on both Twilio and Telnyx
    # inbound webhooks. When True (the default), a missing credential
    # (twilio auth token / telnyx public key) causes the webhook to return
    # 503 Service Unavailable instead of silently accepting the request.
    # Set to False only for local development against mock providers.
    require_signature: bool = True
    # When True, only the very first TTFB event per turn is emitted to the
    # EventBus (matches Pipecat's ``report_only_initial_ttfb`` flag). Default
    # is False to preserve current per-segment emission behaviour.
    report_only_initial_ttfb: bool = False
