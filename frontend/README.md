# Nestock｜窩有 frontend

Android/iOS scene-based Flutter skeleton. See the [root README](../README.md)
for setup, validation, ownership boundaries, and current mock limitations.

The entry point injects AgentClient and CatalogClient. The shipped implementations
are offline demos. The backend Pydantic models are the contract source of truth;
Dart types are provisional display projections without wire serializers.

## C2 offline development

Select a deterministic fixture with `flutter run --dart-define=MOCK_SCENARIO=batchAction`.
Values: `completed`, `singleAction` (default), `batchAction`, `purchaseOptions`,
`partialSuccess`, `error`, `timeout`. Partial success is a display outcome, not an
Agent lifecycle or wire status. Approvals are mock-only and persist nothing.

Future HTTP adapters can receive `AppConfig.local`; override the Android emulator
base URL with `--dart-define=API_BASE_URL=http://10.0.2.2:8000`. For iOS simulator
or desktop host use `http://127.0.0.1:8000`; physical devices need a reachable
host address. No widget or current mock opens a network connection.

`PhotoClient` is an injectable A2 seam; its default reports unavailable and a
future implementation must handle denied permissions. No capture or upload is
implemented. A2/C2 still need to agree upload format, lifecycle, image reference,
Vision invocation and provider. A1/C2 still need to agree request/response mapping,
conversation identity, checkpoint/resume, HITL resume, batch partial approval, and
purchase-option selection. The temporary page navigation and visual design remain
C2 decisions. No client-side approval authorizes backend writes.

For the offline work-surface demo use `--dart-define=MOCK_SCENARIO=visionDemo`;
`visionMany` supplies seven presentation-only items for scrolling checks. Enter
any message to open the panel. Confirmation is a no-write demonstration. These
fixtures are not a Vision or upload contract.

Inventory cabinet and toolbar shortcuts open the same room overlay using a
read-only mock presentation client. Search matches names and aliases locally.
Opening Inventory hides an open Work Surface without discarding its local edits;
closing Inventory reveals it again. A new Agent response closes Inventory. This
is presentation coordination, not backend cancellation or resume semantics.
