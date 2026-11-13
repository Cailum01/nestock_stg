# Nestock｜窩有

> 📖 Want to read this in detail? Ask your AI tool to translate it into
> Traditional Chinese first. Always edit the English original; never save
> a separate Chinese copy.

## Start here

When you open your AI coding tool, paste this first
(replace with your own owner code, name and GitHub account):

> I am B1 任軒 (GitHub: Cailum01). Read README.md and AGENTS.md first
> and follow their rules.

Your scope is listed in "Where each owner extends" below. Branches,
uploads and new folders are handled by your AI tool following AGENTS.md.

Repository: `nestock`. Flutter Android/iOS frontend and FastAPI backend for six
developers. It started as a runnable Skeleton v1 and is now being built into the P0
MVP on real Azure services: Azure OpenAI (Agent and photo recognition), Azure AI
Speech, BigGo price search and Azure Cosmos DB for NoSQL. The Agent answers
Inventory and Shopping questions, looks at photos, looks up prices and queries
forecasts (forecasts read Transaction `created_at` as the time the event happened,
Issue #113; items without enough history return `insufficient_data`). With Cosmos
configured it can also return a purchase confirmation card; nothing is written until
the user confirms it.

## Structure

```text
frontend/
  lib/
    app.dart                    # dependency injection and route composition
    features/agent/              # HTTP and mock clients, scene, results, approval UI
    features/inventory/          # Inventory view (backend over HTTP by default)
    features/shopping/           # Shopping view (backend over HTTP by default)
    features/photo/              # camera capture and photo upload
    features/speech/             # push-to-talk recording and speech client
    shared/                     # config, theme, reusable widgets/states, demo catalog
  android/  ios/                # generated native Flutter targets
  test/                         # widget/interaction tests
  pubspec.yaml  pubspec.lock
backend/
  app/
    api/                        # routes: health, inventory, shopping, forecast,
                                # Agent, media, speech
    agent/                      # LangGraph Agent on Azure OpenAI (A1)
    tools/                      # read adapters using the same domain services
    services/                   # domain services: Cosmos, approvals, vision, speech,
                                # commerce, forecast, shopping; demo data for offline use
    models/                     # authoritative Pydantic models
    core/                       # settings and service composition
    main.py
  tests/
  pyproject.toml  uv.lock
prompts/                        # Agent system prompt
contracts/                      # human-readable contracts and examples
datalake/                       # Cosmos design and database user guide (B1)
deploy/                         # Azure deployment helpers
docs/                           # Skeleton v1 architecture notes and validation
AGENTS.md
```

## Run the frontend

Use Flutter **3.47.7 stable / Dart 3.13.5** (the generation/validation baseline).
Install the Android SDK and accept licenses for Android. iOS requires macOS,
Xcode and an iOS simulator/device. Native identifiers currently use
`com.example.nestock`; C2 must choose production identifiers/signing later.

```sh
cd frontend
flutter pub get
flutter devices
flutter run -d <android-or-ios-device-id>
```

By default the app calls the real backend: the Agent, Inventory, Shopping, photo
upload and speech all use the same backend address. Inventory and Shopping show
whatever the backend reads: Cosmos when it is configured, otherwise demo data. For
the Agent, put the three Azure OpenAI values in `backend/.env` and start (or
restart) the backend; otherwise the app only shows 「暫時無法完成，請重試。」 With
Cosmos configured, the Agent can also return a purchase confirmation card; nothing
is saved until the user confirms it. For offline
UI work only, `--dart-define=AGENT_TRANSPORT=mock` switches the Agent, Inventory
and Shopping to built-in mocks.

The default address `http://10.0.2.2:8000` only works on the Android emulator. A
physical Android phone cannot reach a backend started on `127.0.0.1`. Either
connect it by USB, run `adb reverse tcp:8000 tcp:8000` and pass
`--dart-define=MEDIA_API_BASE=http://127.0.0.1:8000`, or start the backend with
`--host 0.0.0.0` on the same Wi-Fi and pass
`--dart-define=MEDIA_API_BASE=http://<computer LAN IP>:8000`. App users never need
`az login`; only the computer that runs the backend signs in to Azure.

```sh
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
flutter build apk --debug
```

On macOS, also validate `flutter build ios --simulator` and launch on a simulator.
No web or desktop application targets are included.

## Run the backend

Install Python and `uv`. The project supports Python 3.12+; `.python-version`
selects **3.14**, the local validation baseline. `uv sync` can install it if needed.

```sh
cd backend
uv sync --locked
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/health` for `{"status":"ok"}` and
`http://127.0.0.1:8000/docs` for generated interactive API documentation.

| Route | Current behavior on `main` |
| --- | --- |
| `GET /health` | 200; process health, no cloud connectivity check |
| `GET /inventory` | read-only inventory, fixed household scope; Cosmos when all three `NESTOCK_COSMOS_*` are set, otherwise demo data |
| `GET /shopping` | read-only shopping list, fixed household scope; Cosmos when all three `NESTOCK_COSMOS_*` are set, otherwise demo data |
| `GET /forecast` | read-only forecast for active inventory items. Reads transaction history from Cosmos when `NESTOCK_COSMOS_*` are all set, otherwise from demo history (a partial Cosmos config fails at startup). Items without enough history return `insufficient_data`. Forecasts only estimate; they never change confirmed `InventoryItem.quantity` (B2) |
| `GET /forecast/reorder` | read-only list of items worth restocking soon (`consider_restock`), soonest first; empty when none qualify; same history source as `/forecast`; same selection the Agent reuses via `ForecastService.reorder_candidates` (B2) |
| `GET /forecast/{item_id}` | read-only forecast for one inventory item; 404 if missing; same history source and `insufficient_data` behavior as `/forecast`; never changes confirmed quantity (B2) |
| `POST /agent/messages` | Agent via Azure OpenAI when `backend/.env` is set; otherwise 501. With Cosmos it may return a purchase confirmation card, which writes nothing until confirmed |
| `POST /agent/resume` | the user's answer to a confirmation card: writes what the stored card holds (v8 flow C purchases so far); 503 without Cosmos |
| `POST /api/media/upload-authorization` | provisional photo upload authorization (A2, PR #5) |
| `PUT /staging/upload/{upload_token}` | provisional local photo staging upload (A2, PR #5) |
| `POST /api/speech/transcribe` | speech-to-text with Azure AI Speech when `AZURE_SPEECH_KEY` is set; otherwise 503. `NESTOCK_SPEECH_MOCK=1` returns demo text for offline UI work (A2) |

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

### Real data first

Develop and test against the real Azure services. Demo data exists only so the
backend still starts offline and so automated tests stay repeatable.

### Configuration

Copy `backend/.env.example` to `backend/.env` (never commit it), fill in what you
need and restart the backend:

| `.env` section | Used for | When it is empty |
| --- | --- | --- |
| A1 Azure OpenAI (three values) | the Agent | `POST /agent/messages` returns 501 |
| A2 vision (optional) | photo recognition | reuses the A1 Azure OpenAI values |
| A2 Azure AI Speech | speech-to-text | 503; `NESTOCK_SPEECH_MOCK=1` returns demo text |
| A2 BigGo | price search | the Agent uses demo prices and says so |
| Staging base URL (optional) | photo upload address | taken from the request host |

Cosmos is not read from `backend/.env`. Run `az login` on the computer that runs
the backend, then, in the PowerShell window that will start it, set all three
`NESTOCK_COSMOS_ENDPOINT`, `NESTOCK_COSMOS_DATABASE` and `NESTOCK_COSMOS_CONTAINER`
as shown in the [database user guide](datalake/azure-database-user-guide.md). Set
all three or none: a partial set stops the backend from starting, and with none
set Inventory, Shopping and forecasts use demo data. Automated tests expect demo
data, so run `pytest` in a window without these variables. The fixed household id
(`demo-household`) is in `backend/app/core/config.py`.

### Now and target

| | Now on `main` | Target |
| --- | --- | --- |
| App access | the App only calls the backend API and holds no Azure credentials | unchanged |
| Sign-in | the backend uses the developer's own `az login` | if the backend is deployed to Azure, it uses its own Managed Identity with least-privilege access to Cosmos and Blob, so nobody signs in locally |
| Cosmos | optional; read, and written only when the user confirms a card | unchanged |
| Writes | only through confirmation cards: purchases from the Shopping List (v8 flow C, PR #96) | other writes open only after their backend write flow and its approval and validation checks are done; granting write permission on Cosmos alone does not enable writing |
| Photos | kept in backend memory and lost on restart | tracked in Issue #98 |

Azure deployment helpers are in `deploy/`.

## Where each owner extends

| Owner | Member | GitHub | Starting point |
| --- | --- | --- | --- |
| A1 — Agent/HITL | Jason | `jasonko13w` | `app/agent/interfaces.py`, `app/models/agent.py`, `app/tools/` |
| A2 — Vision/naming/commerce | 宗逸 | `YANG-ZONG-YI` | `app/services/vision.py` (Azure OpenAI), `app/services/speech.py` (Azure AI Speech), `app/services/commerce/` (BigGo) |
| B1 — Cosmos/inventory | 任軒 | `Cailum01` | `app/models/domain.py`, `app/services/interfaces.py`, `app/core/dependencies.py` |
| B2 — transactions/forecast | Michelle | `firmwindsor-alt` | `Transaction`, `TransactionHistory`; no inventory write port |
| C1 — shopping/workflows | Judy | `oceanwhileru` | `ShoppingService`, `ShoppingItem`; orchestration remains open |
| C2 — Flutter | 兔毛 | `deerdollfie` | `lib/app.dart`, feature modules, injected `AgentClient`, `InventoryViewClient` and `ShoppingViewClient` |

Normal APIs and Agent tools must use the same domain services. Predicted inventory
must never replace confirmed inventory. Open decisions are tracked as GitHub Issues.
See the [Skeleton v1 architecture notes](docs/architecture.md),
[provisional contracts](contracts/README.md), [contributor rules](AGENTS.md),
[Cosmos design](datalake/azure-data-platform-design.md) and the
[Skeleton v1 validation (2026-10-31)](docs/validation.md).
