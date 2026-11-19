# Android device smoke report

**Date:** 2026-11-18 (Asia/Taipei)<br>
**Target:** Physical Android device; local mock launch followed by a read-only
cloud query.<br>
**App builds:** Initially installed the mock debug APK; replaced it with a
cloud-configured debug APK for the read-only E2E addendum below.<br>
**APK:** `frontend/build/app/outputs/flutter-apk/app-debug.apk`

## Result

| Check | Verdict | Evidence |
|---|---|---|
| ADB device connection | Pass | `adb devices -l` listed `70a58d68`, model `Mi_9T_Pro`. |
| APK install | Pass | `adb -s 70a58d68 install -r <APK>` returned `Success` after USB installation was enabled on the phone. The first attempt was rejected with `INSTALL_FAILED_USER_RESTRICTED`. |
| App launch and foreground | Pass | `monkey` injected one launch event; `pidof com.example.nestock` returned PID `26063`; `dumpsys activity activities` showed `com.example.nestock/.MainActivity` resumed and focused. |
| Agent Home rendering | Pass | Device screenshot showed the Nestock welcome scene, message composer, and camera/microphone controls. Temporary screenshot was removed after inspection. |
| Crash indicators | Pass for this launch only | Captured `AndroidRuntime:E` and `flutter:E` logcat buffers contained no error entries. |
| Send a message / Agent response | Blocked; not run | UI hierarchy exposed `已附加至下一則訊息` and `移除照片` accessibility nodes. No message was sent to avoid unintentionally submitting an attachment. |
| Full API/cloud E2E | Not run | No local backend was listening at `127.0.0.1:8000`; no test deployment was authorized for this run. |

## Commands

```powershell
adb devices -l
adb -s 70a58d68 install -r D:\Dev\Projects\nestock\frontend\build\app\outputs\flutter-apk\app-debug.apk
adb -s 70a58d68 shell monkey -p com.example.nestock 1
adb -s 70a58d68 shell pidof com.example.nestock
adb -s 70a58d68 shell dumpsys activity activities
adb -s 70a58d68 shell dumpsys window
adb -s 70a58d68 logcat -d -t 2000 -s AndroidRuntime:E flutter:E
```

## Follow-up and limits

- The initial mock build's UI tree exposed `已附加至下一則訊息`; no message was
  sent from that build. The cloud-configured build showed no attachment state
  before the read-only query.
- This is not the complete physical-device Demo E2E in
  `demo-readiness-checklist-V3.md` §3 and §6. Purchase, retry/lost-response,
  cancel, `add_stock`, photo approval/retention, Shopping read, forecast, camera,
  and speech flows were not run in this pass.
- No domain data was changed.

## Cloud read-only E2E addendum

**Date:** 2026-11-18 (Asia/Taipei)<br>
**Device:** Xiaomi Mi 9T Pro, Android 11, ADB serial `70a58d68`.<br>
**Cloud target:** `https://nestock-api.ambitiousgrass-c37169f5.eastasia.azurecontainerapps.io`

### Setup and command

Built without the mock transport and with the main HTTPS URL:

```powershell
cd frontend
flutter build apk --debug --dart-define="MEDIA_API_BASE=https://nestock-api.ambitiousgrass-c37169f5.eastasia.azurecontainerapps.io"
adb -s 70a58d68 install -r D:\Dev\Projects\nestock\frontend\build\app\outputs\flutter-apk\app-debug.apk
adb -s 70a58d68 shell monkey -p com.example.nestock 1
```

The read-only readiness request was `GET /health`; it returned HTTP 200 with
`{"status":"ok"}`.

Android interactions:

```powershell
adb -s 70a58d68 shell input tap 350 2220
adb -s 70a58d68 shell input text 'How%smany%spacks%sof%stoilet%spaper%sand%swhere%sis%sit%sstored'
adb -s 70a58d68 shell input tap 970 1361
adb -s 70a58d68 shell input tap 350 2220
adb -s 70a58d68 shell input text 'What%sitems%sare%son%smy%sshopping%slist'
adb -s 70a58d68 shell input tap 970 1361
adb -s 70a58d68 shell input tap 1014 790
adb -s 70a58d68 shell input tap 852 1257
```

Coordinates are specific to this 1080×2296 device layout. UI hierarchy inspection
confirmed each prompt before sending and no attachment state was present.

### Scenario: Inventory read from Android

- **Requirement source:** `demo-readiness-checklist-V3.md` §3.B.
- **Prompt:** “How many packs of toilet paper and where is it stored?”
- **Expected:** Return the correct Inventory item, quantity, and location, without
  an unrelated primary result.
- **Actual:** The app displayed `衛生紙有 8 包，放在浴室櫃子。` The UI hierarchy showed
  the submitted prompt and this response, with an Inventory result card.
- **Verdict:** Pass for this read-only Android → cloud Agent → Inventory query.
- No approval card was shown; no write, cancel, or confirm operation was
  performed.

### Scenario: Shopping list read and UI agreement

- **Requirement source:** `demo-readiness-checklist-V3.md` §3.D; Issues #179/#180.
- **Agent prompt:** “What items are on my shopping list”
- **Agent actual:** `待購清單上有麵粉和牙膏。`
- **Shopping UI actual:** `待購 2 件`, `麵粉`, `牙膏`.
- **Expected:** Agent and Shopping UI show the same pending item names; do not
  expose legacy planned quantities.
- **Verdict:** Pass for the observed two items and absence of displayed planned
  quantities. Null/missing legacy-field variants were not separately exercised.
- No approval card or write operation was performed.

The earlier, ambiguous prompt “What should I buy” returned a forecast/replenishment
answer (`麵粉` and `無糖高纖豆漿`) rather than a Shopping-list answer. It is not
counted as a pass or defect for the Shopping-list requirement.

### Limits

- The health endpoint does not expose the deployed commit/revision; the response
  is not independently attributable to the latest `main` deployment.
- The query was in English, not the exact Traditional Chinese acceptance prompt.
- No write test was run against `demo-household`; its current contents are not a
  pristine baseline (see Issues #126 and #168).
