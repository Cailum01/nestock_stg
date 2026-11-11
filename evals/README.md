# evals/ — Agent 工具選擇測試資料

## 目前內容

- `b2_tool_cases.json`：B2 兩個工具（`get_reorder_candidates`、`get_consumption_forecast`）的
  範例語句與容易混淆的反例。每個工具至少 3 句正面範例，另有反例。對應 v8 第 20 節的工具負責人驗收項目。

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

## 怎麼跑

```bash
cd backend
uv run pytest tests/test_b2_tool_cases.py -q
```

- **離線檢查**（一定會跑）：資料格式、工具名稱是否真的存在於 `TOOL_SCHEMAS`、每個 B2 工具是否
  至少有 3 句正面範例與 1 個反例。
- **真實選工具測試**（只有設好 `backend/.env` 的 Azure 三個值才會跑，否則自動跳過）：把每句話
  送給真的模型，檢查它第一輪選的工具。模型每次回答可能略有不同，失敗時請先重跑一次再判斷。

## 注意

- Golden Prompts 整體由 C1 負責；這裡的檔案格式只是 B2 的提案，C1 可以改格式或併入他的清單。
- v8 把單品預估寫成 `get_consumption_forecast(item_id)`，目前 A1 實作的參數是 `item_name`
  （由後端比對品名）。案例依現有實作寫，改參數時請同步更新 `args_contain`。
- 目前的 Agent 沒有寫入工具，所以「加入購物清單」「買回來了」這類句子要等 C1 的工具完成後，
  才能補上完整的選擇案例。
