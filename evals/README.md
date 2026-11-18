# evals/ — Agent 工具選擇測試資料

## 目前內容

- `b2_tool_cases.json`：B2 兩個工具（`get_reorder_candidates`、`get_consumption_forecast`）的
  範例語句與容易混淆的反例。每個工具至少 3 句正面範例，另有反例。對應 v8 第 20 節的工具負責人驗收項目。

- `a1_tool_cases.json`：庫存、待購、照片、查價四個工具，加上「買回來了」的確認卡工具 `complete_purchase`，以及「家裡多了東西」的確認卡工具 `add_stock`，共 34 個案例，與 B2 案例共用格式。其中 `stock-inc-*` 6 個是「家裡已有的品項加數量」（Issue #206）打開時的案例。

## 欄位

| 欄位 | 意思 |
|---|---|
| `id` | 案例編號 |
| `tool` | 這個案例是為哪個工具寫的 |
| `kind` | `positive`（應該選到）或 `negative`（容易混淆，不該選到） |
| `utterance` | 使用者說的話 |
| `expect_calls` | Agent 第一輪應該呼叫的工具，空陣列表示不應呼叫任何工具 |
| `must_not_call` | 第一輪不能呼叫的工具 |
| `args_contain` | 呼叫時參數需包含的文字（選填） |
| `photo` | `true` 表示附有照片，測試加入 Agent 使用的照片提示（選填） |
| `stock_increase` | 只用在 `add_stock` 的案例：`true` 表示「家裡已有的品項加數量」是打開的（`NESTOCK_STOCK_INCREASE=1`），測試會用打開時的工具說明和系統提示來問模型；沒有這個欄位的案例照關閉時檢查（選填） |
| `shopping_list` | 只用在 `complete_purchase` 的案例：模型第一輪要先查待購清單，測試回給它這幾筆，`expect_calls`、`must_not_call`、`args_contain` 檢查的是下一輪（選填） |

## 怎麼跑

```bash
cd backend
uv run pytest tests/test_b2_tool_cases.py tests/test_a1_tool_cases.py -q
```

- **離線檢查**（一定會跑）：資料格式、工具名稱是否真的存在於 `TOOL_SCHEMAS`、每個 B2 工具是否
  至少有 3 句正面範例與 1 個反例。
- **真實選工具測試**：標記為 `live_eval`，預設 pytest 排除這些案例。設定好
  `backend/.env` 的 Azure 三個值後，明確執行以下指令，將每句話送給模型並檢查第一輪工具。
  缺少設定時會跳過。模型回答可能變動，請保留首輪失敗結果，另記重跑結果與失敗原因。

```bash
uv run pytest -m live_eval tests/test_b2_tool_cases.py tests/test_a1_tool_cases.py -q
```

`expect_calls` 非空時要求列出的工具都出現；空陣列時要求完全沒有工具呼叫。
與其他真實服務測試標記整合時，請保留各標記的預設排除條件。

## 注意

- Golden Prompts 整體由 C1 負責；這裡的檔案格式只是 B2 的提案，C1 可以改格式或併入他的清單。
- v8 把單品預估寫成 `get_consumption_forecast(item_id)`，目前 A1 實作的參數是 `item_name`
  （由後端比對品名）。案例依現有實作寫，改參數時請同步更新 `args_contain`。
- 「買回來了」已有案例（`purchase-*`）；A1 的真模型測試會像有設 Cosmos 的 Agent 一樣，一起提供
  `complete_purchase`。「加入購物清單」的寫入工具還沒有，要等完成後再補案例。
