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
