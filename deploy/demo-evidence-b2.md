# B2 驗收證據（checklist §7 用）

> 日期：2026-11-15。Backend revision：`nestock-api--0000004`（流量 100%，單一副本）。
> Base URL：`https://nestock-api.ambitiousgrass-c37169f5.eastasia.azurecontainerapps.io`
> Household：`demo-household`。以下都是唯讀驗證，沒有寫入或 reset。

| 項目 | 結果 | 證據 |
| --- | --- | --- |
| 雲端 smoke test | PASS | `deploy/smoke_test.py` 六步全過：health、inventory 7 筆、shopping 3 筆、forecast、真實 speech 逐字稿、agent |
| baseline 預檢（loader，不加 `--apply`） | PASS | `action=already_loaded applied=False records_planned=0 documents_deleted=0` |
| AI 端點（本機 `.env` 執行） | PASS | `check_ai_endpoints.py`：`NESTOCK_SPEECH_MOCK` 未設定、OpenAI、Vision、Speech 皆通過 |
| Forecast：洗衣精 | PASS | `estimated`、`consider_restock`、剩約 3 天 |
| Forecast：衛生紙 | PASS | `estimated`、`none`、8 包、剩約 40 天 |
| Forecast：資料不足 | PASS | 電池、鹽、糖為 `insufficient_data`，無編造天數 |
| Agent：「衛生紙還能用多久？」 | PASS | 「目前有 8 包，預估還能用約 40 天」，無 `pending_approval` |
| Agent：「最近有什麼快用完？」 | PASS | 麵粉（0 包）、洗衣精（剩 1 瓶、約 3 天）；另 3 項紀錄不足 |
| 預測不覆寫庫存 | PASS | `app/services/forecast.py` 只讀庫存數量（`confirmed_quantity=item.quantity`），沒有寫入；forecast 相關測試 70 passed；雲端 `/inventory` 數量仍等於 baseline（洗衣精 1、衛生紙 8） |
| 舊 admin 密碼 secret 已移除 | PASS | 已移除 Container App secret `nestockacr26530azurecrio-nestockacr26530`（registry 設定與環境變數都沒有引用它）；revision 仍為 `0000004`，移除後 smoke test 六步全過 |
| ACR 安全 | PASS | `adminUserEnabled=false`；以 pull-only token `nestock-api-pull` 拉取（scope 僅 `content/read`、`metadata/read`，密碼到期 2026-12-15） |

## 尚未驗證

- **Live reset**：#126 尚無 data owner 授權，不執行 `--apply --reset`。
- **雲端執行環境的 Vision**：`check_ai_endpoints.py` 是在本機跑的；雲端的 Vision 連線尚無獨立證據（雲端 Agent 與 Speech 已通過）。
- **Android 實機、C2 是否已收到並設定 base URL**。
- **部署 owner 確認**（checklist 仍標示 tentative）。
