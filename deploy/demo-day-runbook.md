# Demo 當天 Runbook（B2）

> 對應 `demo-readiness-checklist-v3.md` §5（baseline / reset）與 §6（Demo-day Preflight）。
> 主持人：B2。簡報者在台上**不操作任何後端終端機**（checklist §8 第 7 點），
> 所有腳本都在開場前跑完。
> 狀態：雲端已上線（Azure Container Apps `nestock-api`，單一副本）。
> `<BASE_URL>` = `https://nestock-api.ambitiousgrass-c37169f5.eastasia.azurecontainerapps.io`
> （公開網址，不含金鑰）。baseline 的重設仍等 B1 loader（PR #131）與 #126 資料處置授權，
> 在那之前彩排會改動雲端資料且無法還原。

## 0. 原則

1. **任何一步失敗就停**，找出原因再繼續。不以 mock、截圖或「上次是好的」補位。
2. 腳本與文件**不放任何金鑰**。金鑰只在雲端執行環境與 `backend/.env`（不入 git）。
3. 重設是破壞性動作：只在**沒有人在使用後端**時做（排練中、簡報中都不做）。
4. 每次破壞性情境（購買、取消）排練完，重設一次再進下一輪（checklist §5）。

## 1. 工具一覽

| 工具 | 用途 | 會不會寫資料 |
| --- | --- | --- |
| `deploy/check_ai_endpoints.py` | 目前的 OpenAI、Vision、Speech endpoint 真實可達，且 `NESTOCK_SPEECH_MOCK` 未設定 | 否（3 次極小的 API 呼叫） |
| `python -m app.services.demo_seed --manifest <manifest>`（B1，PR #131） | **不加 `--apply`**：唯讀。分區與 baseline 完全相同回 `action=already_loaded`；不同或非空則拒絕並報錯 | 否 |
| `python -m app.services.demo_seed --manifest <manifest> --apply --reset --confirm-household demo-household`（B1） | 重設：同一個 household 的**單一 transactional batch** 刪除現有文件並建立 baseline，再讀回驗證 | **是（破壞性）** |
| `deploy/smoke_test.py --base-url <BASE_URL>` | 打雲端 API 分層驗證（L0 版本、L1 服務（含 `/agent/open-card`）、L2 不變量、L3 拍照卡片），每項各自回報；加 `--write --yes` 才跑會寫入的 L4 | 預設否（L3 暫存一張待確認卡，留著不取消）；L4 會寫入 `demo-household` |

重設與讀回**一律使用 B1 的 loader**，B2 不另寫刪除或寫入實作。loader 隨 PR #131 合併後才可用；
合併前本節的 loader 指令無法執行，Demo 當天不得改用其他方式寫入 `demo-household`。

Cosmos 設定來自 `NESTOCK_COSMOS_ENDPOINT/DATABASE/CONTAINER`（loader 只讀 shell 環境變數，
本機可 `set -a; source backend/.env; set +a`），認證用 `az login`（或有資料角色的 managed identity）。
baseline 來源是 `data/demo_household_manifest.json`（B2 PR #133；合併前用其他路徑時自行指定）。

> **執行任何 `--apply --reset` 之前的前置條件（#126）**：reset 會刪除分區內**所有**文件，
> 包含 `user_input`、media mapping、approval、receipt。必須先有 #126 上對「現有資料可清除」的
> 明確授權（資料處置決議），`--confirm-household` 只確認目標 household，不能代替該決議。
> 沒有授權就只能跑唯讀（不加 `--apply`）。

## 2. 時間線

### D-1（前一天）

- [ ] 確認雲端 backend 在線、單一副本（最小 = 最大 = 1），Demo 前不再重啟。
- [ ] 確認 ACR 沒有啟用 admin user：`az acr show -n <ACR> --query adminUserEnabled` 應為 `false`
  （舊版腳本曾啟用，需關閉：`az acr update -n <ACR> --admin-enabled false`）。
- [ ] 若 app 使用 pull-only token：確認它的密碼到期日晚於 Demo 日，否則先輪替。
  查詢指令見 §6「ACR pull token」（查詢不會輪替）。
- [ ] 確認執行環境變數齊全（只看**名稱**是否存在，不看值）：
  `AZURE_OPENAI_ENDPOINT/_API_KEY/_DEPLOYMENT`、`AZURE_SPEECH_KEY/_REGION`、
  `NESTOCK_COSMOS_ENDPOINT/_DATABASE/_CONTAINER`；
  **`NESTOCK_SPEECH_MOCK` 必須不存在**（不是設成 0，是不存在）。
- [ ] 完整跑一次 §3 → §4 → §5，再做一輪完整情境排練，最後重設回 baseline。
- [ ] 把 `<BASE_URL>` 傳給 C2，實機指向該網址。

### D-day：開場前 60 分鐘

依序執行，每步都要綠燈才進下一步。

```bash
cd backend
# 3. AI 端點：用「目前的」endpoint 真實呼叫
uv run python ../deploy/check_ai_endpoints.py
```

預期：四行 `PASS`（NESTOCK_SPEECH_MOCK unset、Azure OpenAI、Vision、Speech）。

```bash
# 4. 重設到 baseline（破壞性；確認沒有人在用）
M=../data/demo_household_manifest.json
# 先唯讀：看目前分區與 baseline 的關係（already_loaded 代表已經是 baseline，不必重設）
uv run python -m app.services.demo_seed --manifest $M
# 僅在 #126 已授權、且確實不是 already_loaded 時：
uv run python -m app.services.demo_seed --manifest $M --apply --reset --confirm-household demo-household
```

預期：loader 輸出 `action=reset applied=True ... records_created=31`，且沒有 `DemoSeedError`
（庫存 7、待購 3、購買交易 21）。loader 寫入後會自行讀回驗證；任何不符都會報錯。

```bash
# 5. 對雲端 API 做 smoke test
cd ..
python3 deploy/smoke_test.py --base-url <BASE_URL>
```

預期：`N passed, 0 failed`（SKIP 不算通過，要看原因）。可用 `--only L1,L3` / `--skip L2` 選層；`--write --yes` 才會跑 L4（真的 +N，不還原）。

- [ ] 勾選 checklist §6「Cloud / Backend」與「Demo data」各項，並把輸出貼進 §7 證據表
  （revision、日期、loader 輸出行、smoke 結果）。

### D-day：開場前 15 分鐘（Android 實機）

- [ ] 安裝的 App build 與 `<BASE_URL>` 正確（不是 localhost / 10.0.2.2）。
- [ ] 手機網路正常、相機與麥克風權限已授予、電量與充電。
- [ ] 手機上實測一次拍照與語音（checklist §6 Android）。
- [ ] 再跑一次唯讀的 `demo_seed --manifest $M`（不加 `--apply`）；要 `already_loaded`，否則依 §4 處理。

### 簡報中

- 不操作後端、不重啟、不重設。
- 若 App 顯示錯誤：先看 §6 對照表判斷，**不要在台上重啟 backend**。

### 簡報後 / 每輪排練後

```bash
cd backend
M=../data/demo_household_manifest.json
uv run python -m app.services.demo_seed --manifest $M --apply --reset --confirm-household demo-household
```

## 3. 各腳本失敗時怎麼辦

| 失敗訊息 | 可能原因 | 處理 |
| --- | --- | --- |
| `check_ai_endpoints` OpenAI `HTTP 401/403` | 金鑰錯或過期 | 向 A1 取新值，更新執行環境，重跑 |
| `check_ai_endpoints` OpenAI `HTTP 404` | deployment 名稱錯 | 對照 Foundry 的 deployment 名稱 |
| `check_ai_endpoints` Vision 失敗但 OpenAI 通過 | 該 deployment 不支援圖片輸入 | 通知 A2：設 `AZURE_OPENAI_VISION_DEPLOYMENT` 指向支援視覺的部署 |
| `check_ai_endpoints` Speech 失敗 | key / region / endpoint 不符 | 通知 A2 |
| `NESTOCK_SPEECH_MOCK is set` | 環境殘留 | 從環境與 `backend/.env` **刪除**該變數，重啟前先確認不在簡報中 |
| loader 報 `demo-household is not empty or does not exactly match` | 分區有非 baseline 的資料 | 先確認資料處置（#126），再用 `--apply --reset`；不得手動逐筆刪除 |
| loader 報 `requires N Cosmos transactional batch operations ... limit` | 現有文件 + baseline 超過 100 個操作 | 沒有任何寫入發生；通知 B1，**不要**改用分批刪除 |
| loader 的 batch 失敗（逾時 / 衝突） | 單一 batch 是原子的，**舊的完整狀態被保留** | 先唯讀確認狀態，再重跑同一指令；連續失敗通知 B1 |
| loader 報 `without the expected read-back state` | 寫入後讀回與 baseline 不同 | 通知 B1，不開始 Demo |
| smoke L1 `/health` `cannot reach` | backend 沒起來 / 網址錯 | 查託管服務狀態與 `<BASE_URL>` |
| smoke L2 不變量失敗（`category`、`price`、數量為負、重複 ID） | 資料或程式違反專案規則，不是排練殘留 | 通知 B1；排練殘留不會讓 L2 失敗 |
| smoke L2 `/forecast` 失敗（`confirmed_quantity` 與庫存不同、狀態不明） | 預測時間語意未啟用（G5）或歷史未載入 | 先 `--check` 確認歷史在，再通知 B2 / B1 確認 `created_at` 語意設定 |
| smoke L1 speech `MOCK transcript` | 雲端環境有 `NESTOCK_SPEECH_MOCK` 或沒設 Speech | 修正雲端環境變數 |
| smoke L1 agent 回空答案，或 L3 / L4 `no approval card` | Agent 失敗；訊息若寫 `is open (stored; the response hid it)`，代表回應沒有卡但雲端存了一張 | 通知 A1；那張卡不會被取消，下一次拍照會回同一張，L4 會確認它 |
| smoke L0 `behind N commits` | 雲端不是最新 main | 從乾淨的 main 跑 `deploy/deploy-aca.sh` |
| smoke L4 `needs a photo of ONE item` | 家庭有一張別的未決卡（30 分鐘有效），拍照回的是那張舊卡 | 等它過期，或在 App 確認／取消；測試沒有確認它 |

## 4. 重設的範圍與限制

- **會清**：`demo-household` 分區內**所有**文件——庫存、待購、交易、`user_input`、media mapping、
  `pending_approval`、`operation_receipt`、change event。其他 household 不動。
  因此執行前必須有 #126 的資料處置授權（見 §1）。
- **原子性由 B1 loader 保證**：刪除與建立在同一個 household transactional batch，
  失敗時保留舊的完整狀態，不會留下半套或空分區。超過 batch 上限時整個不送出。
- **載入的內容**：manifest 的 31 筆（庫存 7、待購 3、交易 21），不帶 operation receipt。
- **伺服器記憶體不歸它管**：暫存上傳 token 存在 backend 記憶體（`demo-api-reference.md` G4）。
  若排練留下暫存上傳，需重啟單一副本——只能在開場前做，重啟後再跑 smoke test。
- **B2 不寫入**：本 repo 的 B2 腳本只讀（smoke test、AI 端點檢查）；資料寫入只有 B1 loader 這一條。

## 5. Go / No-Go

**Go**（全部成立才開始）：

1. `check_ai_endpoints.py` 四行 PASS。
2. B1 loader 唯讀執行回 `action=already_loaded`。
3. `smoke_test.py` 沒有 FAIL（SKIP 要看原因）。
4. 手機實測拍照與語音成功，且指向 `<BASE_URL>`。

**No-Go**：任何一項不成立。處理順序：看 §3 → 修 → 從失敗的那一步重跑 → 仍失敗則向對應 owner
（A1 Agent / A2 Vision・Speech / B1 Cosmos / C2 App）回報，並在 Demo Script 明示
受影響的情境改為「未驗收」，**不用 mock 或預錄內容冒充**。

## 6. ACR pull token（輪替與撤銷）

背景：學生角色只能把角色指派給 User，無法對 Container App 的 identity 指派 AcrPull，
因此 `deploy-aca.sh` 在授權被拒時改用 pull-only token `nestock-api-pull`
（scope 只有 `nestock-api` 的 `content/read`、`metadata/read`；ACR admin user 保持關閉）。
密碼 30 天到期，**只存在 Container App 的 registry 設定**，不進 repo、CI 輸出或 log。
下列指令中的 `<ACR>`、`<RG>` 用 `deploy-aca.sh` 開頭印出的 registry 與 resource group。

**查詢（唯讀，不輪替）**

```bash
# scope 必須只有 content/read 與 metadata/read
SCOPE_MAP="$(az acr token show -n nestock-api-pull -r <ACR> --query scopeMapId -o tsv | xargs basename)"
az acr scope-map show -n "$SCOPE_MAP" -r <ACR> --query "actions" -o json
# 密碼到期日
az acr token show -n nestock-api-pull -r <ACR> --query "credentials.passwords[].expiry" -o tsv
# admin 必須為 false
az acr show -n <ACR> --query adminUserEnabled -o tsv
```

**輪替**（到期前，或懷疑外洩時）：重跑 `deploy/deploy-aca.sh`。授權被拒時它會重新產生
密碼並更新 Container App 的 registry 設定，舊密碼即失效；新 revision 就緒才算完成。
不要在終端機另外 `credential generate` 後貼到別處，避免密碼留在 shell 歷史。

**撤銷**（立即停止拉取，不影響執行中的 revision）：

```bash
az acr token update -n nestock-api-pull -r <ACR> --status disabled
# 要永久移除：
az acr token delete -n nestock-api-pull -r <ACR> --yes
```

撤銷後，新 revision 或重啟需要拉取映像時會失敗，所以 Demo 前不要撤銷；撤銷後必須
重新執行 `deploy-aca.sh`（會重建 token 並更新設定）才能再部署。若日後取得 AcrPull
指派權限，改用 managed identity 後再刪除 token。

## 7. 任何成員都能重現部署；主要與備用實例

決議（2026-11-15）：雲端部署由任何成員可重現，**執行環境須為 Linux、macOS 或 Windows 的 WSL**
（見下方「執行環境」）。**主要實例在 B2**（本文件的 `<BASE_URL>`），
**B1 維護一個備用實例**。不對任何人開放 B2 的 Azure 訂閱：每個人用自己的帳號登入、
用自己的訂閱與自己的 endpoint 部署，所以金鑰不會因為授權而互相看見。

| 角色 | 負責 | 說明 |
| --- | --- | --- |
| B2 | 主要實例 | Demo 預設網址；token 輪替、revision 與 smoke test |
| B1 | 備用實例 | 在 B1 自己的訂閱部署，Demo 前保持可用並自行跑 smoke test |
| B1 | Demo 當天 smoke test | 開場前由 B1 對 `<BASE_URL>` 跑 `deploy/smoke_test.py`，全綠才開場，結果回報簡報者 |
| 其他成員 | 可重現 | 依下列步驟能自己部署一份，用於排練或接手 |

### 任何成員自己部署一份

**執行環境**

- 已驗證：Linux（自動化測試，使用假的 `az`，24 項通過）。2026-11-15 已對雲端實跑通過。
- 預期可用、尚未驗證：macOS、Windows 的 WSL（Ubuntu）。
- **不支援 Windows 的 Git Bash**：B1 實測時，授權步驟回 `MissingSubscription`，沒有進入 token 備援路徑。
- Windows 使用者請改用 WSL：在 PowerShell 執行 `wsl --install`，開啟 Ubuntu 視窗後安裝 Azure CLI，
  再於該視窗執行腳本。Windows 上原本裝的 `az` 在 WSL 裡不會共用。
- 原因：Git Bash 會把以 `/` 開頭的參數（如 `--scope /subscriptions/...`）改寫成
  `C:/Program Files/Git/...`。腳本已設定 `MSYS_NO_PATHCONV=1` 避開，但 Git Bash 沒有逐項驗證
  （例如缺少 rsync 會失敗）。
- 若在 Git Bash 失敗，請不要重試，改用 WSL 或請已部署過的成員協助。

**步驟**

1. `az login`（用自己的帳號），確認 `az account show` 是要部署的訂閱。
2. 準備 `SECRETS_FILE`：一行一個 `KEY=VALUE`，**不進 git**，值用自己的 endpoint 與金鑰：
   `AZURE_OPENAI_ENDPOINT`、`AZURE_OPENAI_API_KEY`、`AZURE_OPENAI_DEPLOYMENT`、
   `AZURE_SPEECH_KEY`、`AZURE_SPEECH_REGION`、
   `NESTOCK_COSMOS_ENDPOINT`、`NESTOCK_COSMOS_DATABASE`、`NESTOCK_COSMOS_CONTAINER`。
   **不要**設定 `NESTOCK_SPEECH_MOCK`。Cosmos 不需要金鑰（用 app 的 system-assigned identity）。
3. 部署：`SECRETS_FILE=<檔案路徑> deploy/deploy-aca.sh`。腳本會自動找訂閱與資源群組、
   沒有 registry 就建立（admin 保持關閉）、能指派 AcrPull 就用 identity，被拒就改用 pull-only token。
4. **請 B1 替新的 identity 指派 Cosmos 資料角色**（`deploy/b1-cosmos-api-integration.md` §3；
   B1 自己部署備用實例時，自己指派即可）。沒有這一步，`/inventory` 會讀不到資料。
5. 驗證：`python3 deploy/smoke_test.py --base-url <新實例的網址>` 沒有 FAIL 才算可用。

### 備用實例（B1）的維持條件

- [ ] 實例存在、單一副本（最小 = 最大 = 1），`/health` 回 200。
- [ ] 讀的是同一個 Demo Cosmos 與 `demo-household`，smoke test 沒有 FAIL。
- [ ] 使用 token 拉取映像的話，確認密碼到期日晚於 Demo 日（見 §6）。
- [ ] 每次主要實例更新程式後，備用實例也更新並重跑 smoke test，兩邊版本要一致。
- [ ] Demo 前把兩個實例的 smoke test 結果（revision、日期）填進證據表。

### 切換規則

- **只在 Demo 開始前切換，簡報進行中不切換。** 暫存上傳與確認卡流程的狀態可能留在單一實例的記憶體，
  中途換實例會讓進行中的流程失效。
- 主要實例 smoke test 失敗、且短時間內修不好，才改指向備用實例，並在 Demo Script 註明。
- 切換需要 App 指向備用網址。**未決**：C2 的 App 是否能不重新打包就更換 base URL，
  由 C2 回覆；在那之前，備用實例只能保證「能起來並通過 smoke test」，不能保證當天能切換。

### 兩個實例共用資料的注意事項

- 兩個實例讀寫同一個 Cosmos，不是各自一份資料。reset 與購買排練會同時影響兩邊。
- 預測不覆寫庫存、確認卡與冪等規則由後端程式保證，與實例無關。

