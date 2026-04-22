# Observability

Patter ships opt-in OpenTelemetry tracing that covers the four hot spots on
the voice pipeline: STT, LLM, TTS, and tool calls.

## Enable tracing

Tracing is disabled by default. Install the optional dependency and set the
env flag:

```bash
pip install 'getpatter[tracing]'
export PATTER_OTEL_ENABLED=1
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # any OTLP/HTTP collector
```

Then call `init_tracing` once at process start — typically from the same
module that creates your `Patter` client:

```python
from getpatter.observability import init_tracing

init_tracing(
    service_name="my-voice-bot",
    otlp_endpoint="http://localhost:4318",
    resource_attributes={"deployment.environment": "staging"},
)
```

If `PATTER_OTEL_ENABLED` is not set, `init_tracing` returns `False` and
every span becomes a no-op — **zero cost** when disabled.

## Emitted spans

| Span name      | Fires                                     | Attributes                                      |
|----------------|-------------------------------------------|-------------------------------------------------|
| `getpatter.stt`   | One per final transcript                  | `getpatter.stt.text_len`, `getpatter.stt.confidence`  |
| `patter.llm`   | One per LLM iteration (incl. tool rounds) | `patter.llm.iteration`, `patter.llm.history_size` |
| `getpatter.tts`   | One per synthesized sentence              | `getpatter.tts.text_len`                           |
| `patter.tool`  | One per tool invocation                   | `patter.tool.name`, `patter.tool.transport`     |

Every span also carries the current `patter.call.id` so you can group by
call in your backend.

## PII hygiene

Patter never exports user utterances, tool payloads, or LLM content as span
attributes. Only sizes, counts, and identifiers are emitted — this keeps
traces safe to ship to a shared Jaeger / Honeycomb / Grafana Cloud instance.

## Shutdown

Call `shutdown_tracing()` during graceful shutdown to flush any pending spans:

```python
from getpatter.observability import shutdown_tracing

shutdown_tracing()
```

## Troubleshooting

* No spans appear → confirm `PATTER_OTEL_ENABLED=1` is set *in the process
  that calls `init_tracing`*. A quick check:

  ```python
  from getpatter.observability import is_enabled
  print(is_enabled())
  ```

* Collector refuses spans → the endpoint defaults to OTLP/HTTP on port 4318.
  If you are running the gRPC-only OTel Collector image, switch to the
  `otel/opentelemetry-collector-contrib` image or flip your collector config
  to enable the HTTP receiver.
