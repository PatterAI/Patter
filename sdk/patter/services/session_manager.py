import logging
from dataclasses import dataclass, field
from typing import Any
from patter.providers.base import STTProvider
from patter.providers.base import TTSProvider

_logger = logging.getLogger("patter")


@dataclass
class CallSession:
    call_id: str; phone_number: str; direction: str; caller: str; callee: str
    stt: STTProvider | None = None; tts: TTSProvider | None = None
    sdk_websocket: Any = None; telephony_websocket: Any = None
    metadata: dict = field(default_factory=dict)

    async def cleanup(self) -> None:
        if self.stt is not None:
            await self.stt.close()
        if self.tts is not None:
            await self.tts.close()
        if self.telephony_websocket is not None:
            try:
                await self.telephony_websocket.close()
            except Exception as exc:
                _logger.debug("Cleanup close error: %s", exc)
        if self.sdk_websocket is not None:
            try:
                await self.sdk_websocket.close()
            except Exception as exc:
                _logger.debug("Cleanup close error: %s", exc)

class SessionManager:
    def __init__(self): self._sessions: dict[str, CallSession] = {}
    def create_session(self, call_id, phone_number, direction, caller, callee) -> CallSession:
        s = CallSession(call_id=call_id, phone_number=phone_number, direction=direction, caller=caller, callee=callee)
        self._sessions[call_id] = s; return s
    def get_session(self, call_id) -> CallSession | None: return self._sessions.get(call_id)
    def remove_session(self, call_id) -> None: self._sessions.pop(call_id, None)
    def find_by_number(self, number) -> list[CallSession]: return [s for s in list(self._sessions.values()) if s.phone_number == number]
