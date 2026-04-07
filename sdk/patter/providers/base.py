"""Base classes for all Patter providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


# === STT ===

@dataclass
class Transcript:
    text: str
    is_final: bool
    confidence: float = 0.0


class STTProvider(ABC):
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def send_audio(self, audio_chunk: bytes) -> None: ...
    @abstractmethod
    async def receive_transcripts(self) -> AsyncIterator[Transcript]: ...
    @abstractmethod
    async def close(self) -> None: ...


# === TTS ===

class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> AsyncIterator[bytes]: ...
    @abstractmethod
    async def close(self) -> None: ...


# === Telephony ===

@dataclass
class CallInfo:
    call_id: str
    caller: str
    callee: str
    direction: str


class TelephonyProvider(ABC):
    @abstractmethod
    async def provision_number(self, country: str) -> str: ...
    @abstractmethod
    async def configure_number(self, number: str, webhook_url: str) -> None: ...
    @abstractmethod
    async def initiate_call(self, from_number: str, to_number: str, stream_url: str) -> str: ...
    @abstractmethod
    async def end_call(self, call_id: str) -> None: ...
