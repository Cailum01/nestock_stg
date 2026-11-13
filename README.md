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

Repository: `nestock`. A clean, runnable Skeleton v1 for six developers: Flutter
Android/iOS frontend and FastAPI backend. This is a shared foundation, not the MVP.
It starts without cloud credentials. All data shown is read-only demo data.

## Structure

```text
frontend/
  lib/
    app.dart                    # dependency injection and route composition
    features/agent/              # client port, mock, scene, results, approval UI
    features/inventory/          # independent Inventory screen
    features/shopping/           # independent Shopping screen
    shared/                     # read-only catalog port and reusable states
  android/  ios/                # generated native Flutter targets
  test/                         # baseline widget/interaction tests
  pubspec.yaml  pubspec.lock
backend/
  app/
    api/                        # health, demo read routes, provisional Agent routes
    agent/                      # runtime protocol; A1 implementation boundary
    tools/                      # read adapters using the same domain services
    services/                   # domain ports and read-only fixtures
    models/                     # authoritative Pydantic models
    core/                       # settings and service composition
    main.py
  tests/
  pyproject.toml  uv.lock
contracts/                      # human-readable contracts and examples
docs/                           # architecture, decisions and verification
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

By default the app now calls the real backend Agent. Before testing, put the three
Azure OpenAI values in `backend/.env` and start (or restart) the backend; otherwise
the app only shows 「暫時無法完成，請重試。」 The Agent can only answer questions for
now; it cannot save anything yet. Inventory and Shopping still show fixed demo data
(`DemoCatalog`). The default address `http://10.0.2.2:8000` only works on the Android
emulator; on a physical phone, run with
`--dart-define=MEDIA_API_BASE=<an address the phone can reach>`.

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

| Route | Skeleton behavior |
| --- | --- |
| `GET /health` | 200; process health, no cloud connectivity check |
| `GET /inventory` | read-only demo inventory, fixed household scope |
| `GET /shopping` | read-only demo shopping list, fixed household scope |
| `GET /forecast` | read-only forecast for active inventory items. Reads transaction history from Cosmos when `NESTOCK_COSMOS_*` are all set, otherwise from demo history (a partial Cosmos config fails at startup). Items without enough history return `insufficient_data`. Forecasts only estimate; they never change confirmed `InventoryItem.quantity` (B2) |
| `GET /forecast/reorder` | read-only list of items worth restocking soon (`consider_restock`), soonest first; empty when none qualify; same history source as `/forecast`; same selection the Agent reuses via `ForecastService.reorder_candidates` (B2) |
| `GET /forecast/{item_id}` | read-only forecast for one inventory item; 404 if missing; same history source and `insufficient_data` behavior as `/forecast`; never changes confirmed quantity (B2) |
| `POST /agent/messages` | Agent via Azure OpenAI when `backend/.env` is set; otherwise 501. With Cosmos it may return a purchase confirmation card, which writes nothing until confirmed |
| `POST /agent/resume` | the user's answer to a confirmation card: writes what the stored card holds (v8 flow C purchases so far); 503 without Cosmos |
| `POST /api/media/upload-authorization` | provisional photo upload authorization (A2, PR #5) |
| `PUT /staging/upload/{upload_token}` | provisional local photo staging upload (A2, PR #5) |
| `POST /api/speech/transcribe` | speech-to-text; returns demo text until a provider is connected (A2, PR #12) |

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The backend starts without any secrets. To enable the Agent, copy
`backend/.env.example` to `backend/.env` and fill in the three Azure OpenAI values;
never commit `backend/.env`. Change local demo configuration in
`backend/app/core/config.py`. Keep development endpoints local; authentication and
deployment are outside this skeleton.

## Where each owner extends

| Owner | Member | GitHub | Starting point |
| --- | --- | --- | --- |
| A1 — Agent/HITL | Jason | `jasonko13w` | `app/agent/interfaces.py`, `app/models/agent.py`, `app/tools/` |
| A2 — Vision/naming/commerce | 宗逸 | `YANG-ZONG-YI` | future service/tool adapters; no provider selected |
| B1 — Cosmos/inventory | 任軒 | `Cailum01` | `app/models/domain.py`, `app/services/interfaces.py`, `app/core/dependencies.py` |
| B2 — transactions/forecast | Michelle | `firmwindsor-alt` | `Transaction`, `TransactionHistory`; no inventory write port |
| C1 — shopping/workflows | Judy | `oceanwhileru` | `ShoppingService`, `ShoppingItem`; orchestration remains open |
| C2 — Flutter | 兔毛 | `deerdollfie` | `lib/app.dart`, feature modules, injected `AgentClient` and `CatalogClient` |

Normal APIs and Agent tools must use the same domain services. Predicted inventory
must never replace confirmed inventory. See [architecture and all open decisions](docs/architecture.md),
[provisional contracts](contracts/README.md), [contributor rules](AGENTS.md), and
[validation evidence](docs/validation.md).
