# Skeleton v1 validation

Validated on **2026-10-31 (Asia/Taipei)** on Windows with Flutter 3.47.7,
Dart 3.13.5, Python 3.14.7, FastAPI 0.162.0 and Pydantic 2.14.2.
Dependency resolutions are recorded in `frontend/pubspec.lock` and `backend/uv.lock`.

| Check | Result |
| --- | --- |
| Dart formatting (`lib`, `test`) | Pass; no formatting changes remaining |
| `flutter analyze` | Pass; no issues |
| `flutter test` | Pass; 7 widget tests |
| `flutter run -d emulator-5554 --debug --no-resident` | Pass; built APK, installed and launched |
| Android screen inspection | Pass; Agent scene, input, photo entry and cabinet/cart buttons rendered |
| Android target | Existing x86_64 emulator, Android 16 / API 36 |
| `ruff check .` | Pass |
| `ruff format --check .` | Pass; 20 Python files formatted |
| `python -m pytest -vv -s` | Pass; 19 tests |
| Uvicorn startup | Pass; local server at 127.0.0.1:8765 |
| Live HTTP `GET /health` | 200 with `{"status":"ok"}` |
| `git diff --check` | Pass; only Git LF/CRLF conversion notices |
| iOS build/launch | Not run; requires macOS/Xcode |

Frontend tests cover scene navigation and photo placeholder, approve/reject with
unchanged demo data, client injection, loading, duplicate-send guard, error retry,
stable approval identity/decision on resume retry, list empty/error states, and
late response after disposal. These do not prove persistent HITL correctness;
no such implementation exists yet.

Backend tests cover health/OpenAPI, shared API/tool service behavior, scope
isolation, dependency overrides, fail-closed Agent/resume and unchanged data,
request validation, required domain semantics, prohibited fields, finite quantities,
transaction reasons and the provisional result/approval concepts.

The first Flutter run exposed a list retry callback returning a Future from
`setState`; this was corrected and the tests rerun successfully. Test scrolling
was also settled before tapping offscreen controls. Backend TestClient stalled
inside the restricted execution sandbox; the same tests passed outside it.

Non-blocking tool warnings: Starlette warns that its httpx TestClient integration
is deprecated in favor of httpx2; the locked dependency combination passes all
tests. Android startup emits upstream Java native-access and Flutter engine-flag
deprecation notices. No app exception was observed during launch.

The demo app makes no backend/cloud requests. iOS signing, native build and runtime
verification remain a macOS follow-up; generated iOS source is not proof of an
iOS launch. Release packaging/signing and production deployment are out of scope.

Testing references: [Flutter testing overview](https://docs.flutter.dev/testing/overview)
and [FastAPI TestClient documentation](https://fastapi.tiangolo.com/tutorial/testing/).
