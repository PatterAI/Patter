# Calls API Integration Guide

Connect web apps, mobile apps, or custom telephony stacks to your Line agent via the Calls API WebSocket protocol.

## Use Cases

- **Web Applications**: Browser-based voice interfaces using WebRTC/getUserMedia
- **Mobile Apps**: Native iOS/Android apps with audio capture
- **Custom Telephony**: BYO SIP/WebRTC infrastructure, contact center integration

## Authentication

### 1. Get an Access Token

Request a short-lived token from your backend:

```bash
curl -X POST https://api.cartesia.ai/agents/access-token \
  -H "X-API-Key: YOUR_CARTESIA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "your-agent-id"}'
```

Response:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "expires_in": 300
}
```

### 2. Connect via WebSocket

```javascript
const ws = new WebSocket(
  `wss://api.cartesia.ai/agents/stream/${agentId}`,
  {
    headers: {
      "Authorization": `Bearer ${accessToken}`,
      "Cartesia-Version": "2025-04-16"
    }
  }
);
```

### Event Types

#### `start` - Initiate the Call

Send immediately after connection to configure the session.

- `config` overrides your agent's default input audio settings
- `stream_id` is optional. If not provided, the server generates one and returns it in the `ack` event

```json
{
  "event": "start",
  "stream_id": "unique-stream-id",
  "config": {
    "input_format": "pcm_16000",
    "voice_id": "your-voice-id"
  },
  "agent": {
    "system_prompt": "You are a helpful assistant.",
    "introduction": "Hello! How can I help you today?"
  },
  "metadata": {
    "user_id": "user-123",
    "session_id": "session-456"
  }
}
```

#### `media_input` - Send Audio to Agent

Stream user audio to the agent:

```json
{
  "event": "media_input",
  "stream_id": "unique-stream-id",
  "media": {
    "payload": "base64-encoded-audio-data"
  }
}
```

#### `media_output` - Receive Agent Audio

Agent responses come as audio chunks:

```json
{
  "event": "media_output",
  "stream_id": "unique-stream-id",
  "media": {
    "payload": "base64-encoded-audio-data"
  }
}
```

#### `ack` - Server Acknowledgment

Confirms stream configuration. Returns the server-generated `stream_id` if one wasn't provided in the `start` event.

```json
{
  "event": "ack",
  "stream_id": "unique-stream-id",
  "config": {
    "input_format": "pcm_16000",
    "voice_id": "your-voice-id"
  },
  "agent": {
    "system_prompt": "You are a helpful assistant.",
    "introduction": "Hello! How can I help you today?"
  }
}
```

#### `clear` - Interruption Signal

Sent when the user interrupts the agent (barge-in). Stop playing current audio:

```json
{
  "event": "clear",
  "stream_id": "unique-stream-id"
}
```

#### `dtmf` - DTMF Tones

Send DTMF (dual-tone multi-frequency) tones to the agent:

```json
{
  "event": "dtmf",
  "stream_id": "unique-stream-id",
  "dtmf": "5"
}
```

Valid values: `"0"` - `"9"`, `"*"`, `"#"`

## Audio Formats

| Format | Sample Rate | Encoding | Use Case |
|--------|-------------|----------|----------|
| `mulaw_8000` | 8kHz | mu-law | Telephony (lowest bandwidth) |
| `pcm_16000` | 16kHz | 16-bit PCM | Standard quality |
| `pcm_24000` | 24kHz | 16-bit PCM | High quality |
| `pcm_44100` | 44.1kHz | 16-bit PCM | Studio quality |

Choose based on your bandwidth and quality requirements. `pcm_16000` is recommended for most web/mobile applications.

## JavaScript Example

Complete browser integration:

```javascript
class CartesiaVoiceClient {
  constructor(agentId, accessToken) {
    this.agentId = agentId;
    this.accessToken = accessToken;
    this.ws = null;
    this.streamId = null;
    this.audioContext = null;
    this.mediaStream = null;
  }

  async connect() {
    // Connect to Cartesia with auth via headers
    this.ws = new WebSocket(
      `wss://api.cartesia.ai/agents/stream/${this.agentId}`,
      {
        headers: {
          Authorization: `Bearer ${this.accessToken}`,
          "Cartesia-Version": "2025-04-16",
        },
      }
    );

    this.ws.onopen = () => {
      // Send start event to initialize the stream
      this.ws.send(JSON.stringify({
        event: "start",
        config: {
          input_format: "pcm_16000",
        },
        agent: {
          system_prompt: "You are a helpful assistant.",
          introduction: "Hello! How can I help?"
        }
      }));
    };

    this.ws.onmessage = (event) => {
      const message = JSON.parse(event.data);

      switch (message.event) {
        case "media_output":
          this.playAudio(message.media.payload);
          break;
        case "clear":
          this.stopPlayback();
          break;
        case "ack":
          this.streamId = message.stream_id;
          console.log("Stream initialized:", this.streamId);
          break;
      }
    };

    // Start capturing microphone
    await this.startMicrophone();
  }

  async startMicrophone() {
    this.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.audioContext = new AudioContext({ sampleRate: 16000 });

    const source = this.audioContext.createMediaStreamSource(this.mediaStream);
    const processor = this.audioContext.createScriptProcessor(4096, 1, 1);

    processor.onaudioprocess = (e) => {
      if (this.ws?.readyState === WebSocket.OPEN && this.streamId) {
        const pcmData = e.inputBuffer.getChannelData(0);
        const int16Data = this.floatTo16BitPCM(pcmData);
        const base64 = btoa(String.fromCharCode(...new Uint8Array(int16Data.buffer)));

        this.ws.send(JSON.stringify({
          event: "media_input",
          stream_id: this.streamId,
          media: { payload: base64 }
        }));
      }
    };

    source.connect(processor);
    processor.connect(this.audioContext.destination);
  }

  floatTo16BitPCM(float32Array) {
    const int16Array = new Int16Array(float32Array.length);
    for (let i = 0; i < float32Array.length; i++) {
      const s = Math.max(-1, Math.min(1, float32Array[i]));
      int16Array[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return int16Array;
  }

  playAudio(base64Data) {
    // Decode and play audio
    const binaryString = atob(base64Data);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
      bytes[i] = binaryString.charCodeAt(i);
    }
    // Queue audio for playback...
  }

  stopPlayback() {
    // Stop current audio playback on interruption
  }

  disconnect() {
    this.mediaStream?.getTracks().forEach(track => track.stop());
    this.ws?.close();
    this.audioContext?.close();
  }
}

// Usage
const client = new CartesiaVoiceClient("agent-id", "access-token");
await client.connect();
```

## Connection Management

### Keepalive

Send standard WebSocket ping frames to prevent inactivity timeouts:

```javascript
// Requires the Node.js `ws` library — the browser WebSocket API does not expose ping()
setInterval(() => {
  if (ws.readyState === WebSocket.OPEN) {
    ws.ping();
  }
}, 60000); // Send ping every 60 seconds
```

### Timeouts

- **Idle timeout**: 180 seconds of no messages before the server closes the connection. Any client message (media_input, dtmf, custom events, or WebSocket ping frames) resets the timer.

### Error Handling

```javascript
ws.onerror = (error) => {
  console.error("WebSocket error:", error);
};

ws.onclose = (event) => {
  console.log("Connection closed:", event.code, event.reason);

  if (event.code === 1000 && event.reason === "connection idle timeout") {
    // Reconnect and resend start event
  } else if (event.code === 1000) {
    // Normal closure — agent ended the call
  }
};
```

### Close Reasons

The server closes connections with code `1000` (Normal Closure). Check the `reason` field to distinguish:

| Reason | Description |
|--------|-------------|
| `"call ended by agent"` | The agent ended the call. May include additional context: `"call ended by agent, reason: {details}"` |
| `"connection idle timeout"` | No messages received for 180 seconds |

## Mobile Integration

### iOS (Swift)

Use `URLSessionWebSocketTask` for WebSocket connections and `AVAudioEngine` for audio capture/playback.

### Android (Kotlin)

Use OkHttp WebSocket client and `AudioRecord`/`AudioTrack` for audio handling.

### React Native

Use `react-native-websocket` and `expo-av` or `react-native-audio-api` for cross-platform audio.

## Best Practices

1. **Send `start` first** — The connection closes if any other event is sent before `start`.
2. **Choose the right audio format** — Match the format to your source: `mulaw_8000` for telephony, `pcm_44100` for web clients.
3. **Handle closes cleanly** — Always capture close codes and reasons for debugging and recovery.
4. **Keep the connection alive** — Send WebSocket ping frames every 60–90 seconds to avoid the 180-second inactivity timeout.
5. **Manage stream IDs** — Provide your own `stream_id` values to improve observability across systems.
6. **Recover from idle timeouts** — On `1000 / connection idle timeout`, reconnect and resend a `start` event.
