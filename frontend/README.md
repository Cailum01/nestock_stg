# Nestock｜窩有 frontend

Android/iOS scene-based Flutter application. See the [root README](../README.md)
for setup, validation, and ownership boundaries.

The entry point injects `AgentClient` and `CatalogClient`. By default,
`HttpAgentClient` sends messages to the backend `POST /agent/messages` endpoint.
It preserves structured result data for presentation and renders inventory items
when returned. Photo-aware requests can forward an uploaded, opaque `image_ref`.
Backend Pydantic models remain the contract source of truth; Dart view types and
the current HTTP mapping are provisional.

## C2 development

For Android Emulator, the HTTP clients default to `http://10.0.2.2:8000`.
Override the base with `--dart-define=MEDIA_API_BASE=http://127.0.0.1:8000`
for an iOS simulator or local host, or use a reachable host address on a physical
device. Camera capture and photo upload use injectable clients; successful uploads
retain the returned `image_ref`. The composer also supports push-to-talk recording
and sends audio to `POST /api/speech/transcribe`; the transcript is inserted into
the input for review and manual sending.

For local Agent fixtures, use `--dart-define=AGENT_TRANSPORT=mock` and optionally
select `--dart-define=MOCK_SCENARIO=batchAction`. Scenarios are `conversation`
(default), `completed`, `singleAction`, `batchAction`, `purchaseOptions`,
`partialSuccess`, `error`, `timeout`, `visionDemo`, and `visionMany`. These are
presentation fixtures, not backend lifecycle or wire statuses. Selecting the mock
Agent does not disable the independently configured photo or speech HTTP clients.

Enter `/vision-demo` to open the local Vision review fixture. With mock transport,
`visionMany` provides seven items for scrolling checks. Ordinary conversation does
not automatically open the fixture. Confirmation remains a no-write demonstration;
the fixture is not a Vision or upload contract.

The HTTP client does not implement `/agent/resume` and rejects pending approvals.
Persistent HITL writes and Cosmos writes are not implemented in the frontend;
Vision confirmation/write flow is not complete. No client-side approval authorizes
backend writes.

Inventory cabinet and toolbar shortcuts open the same room overlay using a
read-only mock presentation client. Search matches names and aliases locally.
Opening Inventory hides an open Work Surface without discarding its local edits;
closing Inventory reveals it again. A new Agent response closes Inventory. This
is presentation coordination, not backend cancellation or resume semantics.
