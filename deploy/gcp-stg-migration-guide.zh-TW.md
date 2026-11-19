# GCP STG 遷移操作指南（繁體中文）

> 範圍：規劃及人工操作指引；本文件不會建立雲端資源，也不表示目前程式已能使用 Firestore/GCS。操作前請先確認正式核准的專案、資料擁有者、停機窗口及預算。文件中的 `<...>` 均須由操作者自行填入，不是實際資源名稱或 ID。

## 1. 先確認現況與限制

- 目前 FastAPI 使用 Cosmos DB adapter；媒體使用 Azure Blob adapter。設定 `NESTOCK_COSMOS_*`、`AZURE_STORAGE_*` 只會啟用既有 provider，**不能**切換成 Firestore 或 Cloud Storage。
- GCP 目標候選為 Cloud Run（request-based、min instances `0`）、Firestore Standard 及私有 Cloud Storage bucket。必須先完成並驗收 GCP adapters，否則不要把 Cloud Run 設為有狀態 STG，也不要把真實資料寫入未支援的後端。
- 使用者已授權 household/device 身分設計，並明確指定一台裝置可參與多個家庭。這是對舊有「無登入/家庭身分」不變條件的刻意產品變更；目前程式尚未實作。Issue #205 仍保持開啟且未修改；不得把本文件當成 issue 已結案或 code 已完成。
- 首次啟動提供「建立新家庭」或「加入現有家庭」；家庭預設隔離，建立家庭的安裝裝置取得初始 owner membership。每個裝置可有多筆 household membership；加入邀請只新增所屬家庭 membership，不會取代其他家庭或合併/複製資料。role 屬於每個 membership，分為 owner/admin/member。
- owner/admin 可產生限定單一家庭、高熵的一次性 QR/邀請碼（建議至少 128 random bits），預設 10 分鐘過期，伺服器只存雜湊並以原子操作確保只能兌換一次。預設每家庭每小時最多建立 10 個邀請；每安裝及來源每 15 分鐘最多 5 次失敗兌換，之後冷卻 15 分鐘；正式上線前依測試調整。邀請預設新增 member。admin 可撤銷該家庭的 member；owner 可撤銷非 owner membership、授予/撤銷 admin；只有 owner 可轉移 owner 權限。
- 每個安裝有獨立 bearer credential，伺服器只存 token 雜湊，裝置 registry 與 household membership 分開。App 將 token 存入 iOS Keychain / Android Keystore，只經 TLS Authorization header 傳送，不放 URL、log 或一般偏好設定。App 提供家庭切換器，每個家庭資料 API 呼叫都帶所選 `X-Household-Id`；此值只是選擇，不是授權。後端每次須驗證該安裝在所選家庭仍有有效 membership，並以 server-side membership 決定 household scope/role；不得直接信任 request body/path 的 `household_id`。create/join/recovery bootstrap API 不讀取家庭資料，但必須驗證一次性邀請或 recovery challenge。
- member 可離開單一家庭而保留其他 memberships；owner 須先轉移 owner 權限才能離開。owner/admin 移除 membership 只影響該家庭。每張 approval card 和 operation ID 建立時永久綁定 household；確認/重送時須重新驗證目前安裝在原家庭的 membership、卡片 household 及既有核准規則。跨家庭重播 operation ID 或切換家庭後重新套用舊卡片一律拒絕。
- 裝置遺失時，可用該安裝的一次性高熵 recovery code（只顯示一次、伺服器只存雜湊）在新安裝全域撤銷舊 credential 及其所有 memberships；新裝置不自動繼承 memberships，須逐家庭重新受邀。另有家庭限定 recovery code，可在單一家庭恢復 owner membership，不會恢復其他家庭角色。若沒有裝置 recovery code，需各家庭 owner/admin 分別撤銷遺失裝置的 membership；若 owner access 和該家庭 recovery code 都遺失，無法恢復 owner。無密碼或 email 帳戶；憑證不會略過 HITL 寫入核准。
- 現有 `demo-household` 資料不能自動歸入新家庭。舊版 App/資料沒有可沿用的家庭帳戶或裝置憑證；資料移轉須由資料擁有者明確授權並另行驗證。
- Flutter 的 `API_BASE_URL` 和 `MEDIA_API_BASE` 是編譯期設定。新安裝包可在建置時指定；已安裝 App 不會因後端環境變數改變而自動改網址。要免重打包切換，必須使用 App 原本已連線的穩定網域並只更改 DNS/路由；否則須發布新 App，除非另有核准的 runtime 設定功能。

## 2. 費用與區域（官方頁面查核日：2026-11-19）

- **Cloud Run：** request-based 模式在 min instances 設為 `0` 時可縮至零；官方月免費額度列出 180,000 vCPU 秒、360,000 GiB 秒及 200 萬次 request。免費額度會按 billing account 彙總，且區域/層級影響價格。`asia-east1`（台灣）列於 Tier 1。縮零會有冷啟動，免費額度不是費用保證。
- **Firestore Standard：** 每個 project 只有一個符合免費額度的 database；列出的額度為 1 GiB 儲存、每日 50,000 次讀取、20,000 次寫入、20,000 次刪除及每月 10 GiB 對外傳輸。超額、資料位置、備份/PITR、索引與其他功能可能收費。Firestore 位置清單包含台灣 `asia-east1`；建立前仍須在 Console 確認當時方案、位置與費率。
- **Cloud Storage：** Standard Always Free 的 5 GiB-month 及操作額度只適用 `us-west1`、`us-central1`、`us-east1`；台灣 bucket 不適用該免費儲存額度，須預算列入儲存、操作及傳輸費。地區費率會變動。
- **本 STG 僅授權使用有效促銷抵免額：**不授權升級付費帳戶，也不授權支出超出有效抵免額。連結專案前，必須在 Cloud Billing 確認使用者提供的帳務帳戶本身確為符合資格且有效的 Free Trial 帳戶，並核對剩餘抵免額及到期日；未確認前不得連結或建立資源。文件及程式庫不得記錄該帳戶 ID。將現有抵免額視為一次性，除非 Google 確認另有資格。本指南不提供建立帳戶或以帳戶輪替續領抵免額的操作指引，也不支援規避資格條件。若區域或服務費用無法由有效抵免額涵蓋，停止操作。
- Google 說明：抵免額用完或註冊滿 90 天而未升級付費帳戶時，Free Trial 帳戶會關閉且相關專案/資源會停止。此處不授權付費升級；須在抵免額耗盡與試用到期兩者較早之前完成必要資料匯出、驗證與資源停用/清理，不可依賴寬限期或服務持續可用。
- 不要把免費額度、抵免額估算或 Budget alert 當成支出硬上限。Budget alert 只通知、不會封頂或停止支出，且用量/帳單資料可能延遲；保留充足抵免額緩衝並頻繁人工檢查。若無法確保只使用有效抵免額，勿啟用資源。

官方資料：
[Cloud Run 定價](https://cloud.google.com/run/pricing)、
[Cloud Run 最小執行個體](https://cloud.google.com/run/docs/configuring/min-instances)、
[Firestore 定價](https://cloud.google.com/firestore/pricing)、
[Firestore 位置](https://docs.cloud.google.com/firestore/docs/reference/rest/v1/projects.locations/list)、
[Cloud Storage 定價](https://cloud.google.com/storage/pricing)、
[Free Trial / Free Tier](https://docs.cloud.google.com/free/docs/free-cloud-features)、
[Cloud Billing budgets](https://cloud.google.com/billing/docs/how-to/budgets)。

## 3. 90 天內的帳務策略

Google Cloud Free Trial 僅供符合資格的新客戶使用，提供 $300 Welcome credit，期限為註冊起 90 天。本 STG 限定只使用尚有效的促銷抵免額；不授權升級付費帳戶或超出抵免額支出。

**連結前的必要檢查：**先在 Cloud Console 確認使用者提供的 Billing account 狀態確實為符合資格的 Free Trial，而非一般付費帳戶；核對剩餘 Welcome credit、註冊/到期日，以及此帳戶能否依官方規則用於目標 project。不要只憑帳戶名稱、口頭說明或帳號 ID 推定資格。文件不記錄帳戶 ID。將現有抵免額視為一次性，除非 Google 確認另有資格。若帳戶不是有效且合格的 Free Trial、抵免額不足，或 linking 不符合政策，停止；不得升級付費帳戶。若因核准的遷移而更換 billing account/project，目的帳戶及 project 必須依 Google 現行規則獨立符合資格並獲授權；本指南不提供建立帳戶、續領抵免額或規避資格的操作指引，也不應假設新帳戶會取得抵免額。

Google 說明：若 $300 抵免額耗盡或註冊滿 90 天且未升級付費帳戶，Free Trial billing account 會關閉，相關 project/resources 會停止；其後可能有資料保留/寬限流程，但不可依賴。此處不授權付費升級。**立即規劃並在抵免額耗盡與 90 天到期兩者較早前，完成一致性檢查、必要資料匯出、服務停止及資源清理。**不要等到最後一天，預留處理延遲帳務資料與匯出驗證的時間。

Budget alert 只是通知，不是支出硬上限或自動停機機制，且成本資料可能延遲。操作期間保留保守的抵免額緩衝並頻繁檢查 Billing report；不可把預算通知視為確保不超支的唯一控制。

官方步驟及說明：[啟用、停用或更改專案帳務](https://docs.cloud.google.com/billing/docs/how-to/modify-project)；[Free Trial FAQ](https://cloud.google.com/signup-faqs)；[Free Trial 與資源生命週期](https://docs.cloud.google.com/free/docs/free-cloud-features)；[Budget alerts](https://cloud.google.com/billing/docs/how-to/budgets)。

**若必須使用新 project：** 視為資料遷移，不是單純改設定。依第 6 節先建立/驗證目標、匯出/匯入、凍結寫入、比對資料，再切流量；保留可回復來源的時間窗口。

## 4. 前置條件

1. 已核准的 Google Cloud organization/folder、來源/目標 project、管理者與帳務聯絡人；須先確認待連結帳戶確為合格、有效且抵免額足夠的 Free Trial billing account。此 STG 不授權 paid upgrade 或抵免額外支出；不要在文件、命令歷史或聊天中貼入密碼、金鑰、連線字串、個人資料或 billing account ID。
2. `gcloud` CLI 已安裝並以個人帳戶登入；確認目前帳戶、project 與權限。正式操作前請核對所有畫面及命令使用的 project，避免誤操作。
3. 已批准 STG 區域（預設候選 `asia-east1`）、服務名稱、資料保留/備份政策、預算門檻、維護窗口及回復負責人。
4. 已確認資料來源、資料擁有者與是否可複製到新 project；PII / 家庭資料不可為測試而擅自跨環境複製。
5. 已完成 Firestore/GCS adapter、裝置憑證/邀請/recovery API、伺服器 household scope、交易語義與資料遷移的 code review、測試及獨立 QA（如適用）。目前尚未完成時，停在規劃，不要切換流量或承載真實家庭資料。

## 5. 同一 project：建立或核對 GCP 目標資源

本節僅適用於已核准且應用 adapter 已通過驗收的情況。建議透過 Console 操作；用 CLI 前先閱讀官方說明，逐項替換 placeholder。

1. **帳務及 API：**連結前再次核對 billing account 確實是符合資格且仍有效的 Free Trial 帳戶、抵免額足以涵蓋預估用量且未到期；若無法確認，停止，不得改連付費帳戶或升級。只在此條件成立時啟用 Cloud Run、Artifact Registry、Firestore、Cloud Storage、Secret Manager 所需 API。
2. **區域：**選擇並記錄 Cloud Run 與資料位置。優先 `asia-east1`；確認各產品當下可用性、資料所在地要求及費率。資料庫/儲存位置建立後通常不應假設可原地更改。
3. **Firestore：**建立一個符合需求的 Firestore Native / Standard database。預設只用核准的資料庫；核對索引、TTL、備份/PITR 與資料刪除政策。
4. **Cloud Storage：**建立區域性 Standard bucket；開啟 Uniform bucket-level access 及 Public access prevention，不授予 `allUsers`/`allAuthenticatedUsers`。限制可上傳類型/大小和生命週期。需直接上傳時只使用 adapter 核准的短效簽署網址，並按最小來源設定 CORS。
5. **Cloud Run：**部署已審核的映像至核准區域；使用專用 service account、request-based billing、min instances `0`，設定經容量驗證的 max instances、CPU、記憶體、timeout 與 concurrency。先用私有 ingress/IAM 測試；只有產品需要時才開放公開 HTTPS API，且不得把資料庫或 bucket 設為公開。
6. **IAM / Secrets（執行期與部署者分離）：**建立專用 user-managed Cloud Run service identity；Cloud Run 透過 Application Default Credentials 使用此身份，不放 service-account key。**執行期身份**只拿下列指定資料資源的角色，不授予 Owner、Editor、Cloud Run deployer 或 IAM 管理權：
   - **Firestore Standard：**對執行期 service account 授予 `roles/datastore.user`，並以 IAM Condition 限定在單一 database；條件範例為 `resource.name == "projects/<PROJECT_ID>/databases/<DATABASE_ID>"`。此 Google 預定義角色為應用程式/服務帳戶提供 Firestore 資料讀寫，但包含 entity read/write 權限；條件將其限定至核准 database。應用程式仍須自行執行 household membership 與資料隔離。Firestore 文件指出，Cloud Console 不會依 database-level IAM Condition 判定 database 存取；使用 client library / API 存取時才會套用該條件，勿以 Console 測試結果代替驗證。
   - **Cloud Storage 物件：**僅在目標私有 bucket 上授予 `roles/storage.objectUser`，供該 bucket 內物件建立、讀取、更新及刪除；不要授予 project-wide Storage 權限。
   - **Bucket 設定讀取：**目前 adapter 在啟動時讀取 bucket metadata，以檢查 Uniform bucket-level access / Public access prevention；此操作需要 `storage.buckets.get`。可在同一個 bucket 上額外授予 `roles/storage.bucketViewer`（Beta），其官方權限包含 bucket metadata 讀取。若組織不允許 Beta 角色，先停下並請 IAM owner 核定只包含已驗證 `storage.buckets.get` 的替代方案；不可改用 `roles/storage.admin`、Owner 或 Editor。若程式不再讀取 bucket metadata，應重新核對是否仍需要此角色。
   - **Secrets：**只對應用程式實際使用的個別 secret 授予所需讀取權；不要把 secret 管理/建立權限授予執行期身份。不要把 key file、secret 值或 `.env` 放入 repo、image、命令列或日誌。

   **Operator / deployer 是不同 principal：**部署既有 Artifact Registry 映像時，Google 文件列出的 deployer 權限包括 Cloud Run Developer (`roles/run.developer`，限 Cloud Run service)、Service Account User (`roles/iam.serviceAccountUser`，限上述 runtime service account) 及 Artifact Registry Reader (`roles/artifactregistry.reader`，限映像所在 repository)。這些角色只授予執行部署的使用者/CI，不授予 runtime identity。建立資源、設定 IAM bindings 或管理 secrets 的 operator 權限也須獨立核准並限縮；不以 Owner/Editor 取代。

   官方依據：[Cloud Run service identity](https://docs.cloud.google.com/run/docs/securing/service-identity)、[Cloud Run IAM roles](https://docs.cloud.google.com/run/docs/reference/iam/roles)、[部署 container image 的必要角色](https://docs.cloud.google.com/run/docs/deploying)、[Firestore IAM roles and permissions](https://firebase.google.com/docs/firestore/security/iam)、[Firestore database-level IAM Conditions](https://firebase.google.com/docs/firestore/manage-databases#configure_database_access_conditions)、[Cloud Storage IAM roles](https://docs.cloud.google.com/storage/docs/access-control/iam-roles)、[Cloud Storage permissions](https://docs.cloud.google.com/storage/docs/access-control/iam-permissions)。
7. 建立 Cloud Billing budget 與多個通知門檻，通知收件人由資源擁有者確認。另開啟用量、錯誤率、Cloud Run instance、Firestore 讀寫/儲存、bucket 佔用/傳輸告警。Budget alert 不是硬性支出上限，也不保證即時；每日人工確認剩餘抵免額及成本，預留緩衝，並在接近預設安全門檻時立即匯出及清理，不等待 alert 才行動。

避免未核准的常駐執行個體、VPC/NAT、負載平衡器、跨區複寫、PITR、額外備份及大量外傳；這些功能可能造成持續或非預期成本。勿為省錢關閉必要權限隔離或資料保護。

## 6. 新 project：受控資料匯出/匯入與切換

僅在資料擁有者明確批准，且目的帳戶/project 依 Google 現行規則獨立、合法符合資格並獲授權時執行；若要使用促銷抵免額，須先確認目的帳戶的抵免額資格與有效期，不可假設遷移會延長或重新取得抵免額。沒有符合資格/授權的目的地時，停止服務並依計畫匯出及清理，不得以付費帳戶接手或超出抵免額繼續服務。本指南不提供建立/輪替帳戶或規避促銷抵免資格的指引。Firestore 與 Cosmos schema/transaction 語義不同，**不可直接假定 Cosmos backup 可匯入 Firestore**；須採用 adapter 支援並測試的 mapping 工具。

1. **準備：**記錄來源和目標 project/region、資料範圍、文件數/bytes 基準、bucket objects 清單與 checksum、索引/TTL、IAM、secrets、流量路由、停機窗口及 rollback 負責人。先用非敏感樣本演練 restore。
2. **建目標：**依第 5 節建立隔離的 project、Firestore、私有 bucket、Cloud Run service identity、Secrets、索引/保留政策與預算警示。確認沒有公開讀取或寫入權限。
3. **初次複製：**依官方受支援方式匯出來源資料並匯入目標；物件以保留 metadata 的工具複製，逐檔比對 checksum/大小。匯出檔及中介 bucket 也須私有、限權並設定清理期限。不得將資料下載到未加密或共用裝置。
4. **驗證：**比較資料筆數、bytes、關鍵欄位、device registry、membership 關聯及 household scope；抽樣讀取媒體；跑 adapter 自動測試和唯讀 API smoke test。驗證索引、時間欄位、TTL、交易/idempotency、approval card 的 household 綁定、跨家庭重播拒絕及日誌不含秘密。裝置 token 明文不會存在於伺服器匯出檔；只有確認 hash/registry 格式相容且資料擁有者核准後，才遷移 hashes、revocation 狀態和 memberships。若選擇撤銷全部舊 tokens，須安排每台裝置逐家庭重新加入；切換時撤銷未兌換 invites。不要自動將 legacy `demo-household` 指派給新家庭。
5. **切換前凍結：**暫停來源寫入或啟用經批准的寫入凍結，記下最後一致時間點；完成增量匯出/複製並再比對。不要未經設計就雙寫。
6. **切流量：**更新受控路由/DNS或重新建置 App（依第 7 節）；逐步導流。先確認健康檢查及 API/媒體功能，再開啟已批准的寫入。記錄切換時間和 revision。
7. **回復：**若資料或請求錯誤，先凍結新 project 寫入，依預先演練的反向同步/匯出計畫把切換後的必要資料安全地回復至來源，再切回原 hostname/service。不可直接切回舊端點而丟棄新端寫入。記錄資料差異與人工處置。
8. **觀察期後清理：**資料擁有者核准且回復窗口結束後，才刪除暫存匯出檔、舊版本和不再使用的資源；先確認保留/稽核要求。先撤銷舊 service account 權限及 secrets，再刪除資源。每一步需人工核對 project ID 和資源名稱。

Firestore 匯入/匯出的權限、限制及操作依官方文件為準：[匯出與匯入資料](https://cloud.google.com/firestore/docs/manage-data/export-import)。跨 project 的匯入還須核對文件所述 IAM/service-agent 權限。

## 7. 設定切換、部署與驗證

- Backend 現有變數包括 `NESTOCK_COSMOS_ENDPOINT`、`NESTOCK_COSMOS_DATABASE`、`NESTOCK_COSMOS_CONTAINER`、`AZURE_STORAGE_CONNECTION_STRING` / `AZURE_STORAGE_BLOB_ENDPOINT`、Azure AI/Speech 與 BigGo 設定。它們只服務既有整合。**不可把 Firestore 專案 ID 或 GCS bucket 名稱塞進 Cosmos/Azure 變數。**
- Firestore/GCS 只有在 adapter code 合併、明確 provider 設定及測試完成後，才依該版本文件設定新的非秘密識別值和 Secret Manager 參照；此處不猜變數名稱或值。
- household/device API 也尚未實作；部署前確認每個 household-scoped API 都以 per-install token 驗證，並確認所選 `X-Household-Id` 對該安裝有有效 membership。body/path 中的 `household_id` 不能覆寫 server scope。測試 household switch、member leave、owner/admin 權限、邀請 hash/一次兌換/10 分鐘到期/rate limit、device 全域撤銷及單一家庭 recovery。每張 approval card 和 operation ID 都須不可變地綁定建立時的 household；跨家庭 replay 必須拒絕。不得只靠 App 藏起家庭 ID。
- 先建置並驗證映像，再部署 Cloud Run。部署前 `gcloud config get-value project`、帳務頁和 Console project selector 三者需一致；不複製以下 placeholder 直接執行：

  ```sh
  gcloud config set project <APPROVED_PROJECT_ID>
  gcloud run services describe <SERVICE_NAME> --region <REGION>
  ```

- 以 Cloud Run URL/受控 hostname 做 HTTPS smoke test；查看 `/openapi.json` 或經核准的唯讀 API。應用已具備 GCP adapter 後再測試資料讀寫及媒體上傳/讀回/刪除；寫入測試使用合成資料，並確認只作用於隔離 STG。現有 `deploy/smoke_test.py --base-url <URL>` 可在其相依服務/測試資料已備妥時使用，但不能單獨證明 GCP persistence、媒體權限或 household identity 正確。
- 驗證 Cloud Run revision、啟動日誌、錯誤率、Firestore reads/writes、bucket 佔用與拒絕公開存取；核對 API 與媒體網址都指向核准環境。
- 每次部署後記錄版本、設定變更（不含秘密）、smoke test 結果、Cloud Billing 預估/實際用量及回復決策。

## 8. Flutter App 網址切換

建置新的 App 時，依實際 Flutter build pipeline 設定 `API_BASE_URL` 和 `MEDIA_API_BASE` 為 HTTPS hostname；不要在已發布 App 中期待後端環境變數更新這些編譯期值。測試包/正式包不得以 localhost、模擬器網址或臨時 Cloud Run URL 作為長期端點。

若需要已安裝 App 無須重建置即可切換，先確認 App 原本使用穩定 hostname；由有權限的操作者更新其 DNS/路由，保留舊服務至 DNS/連線觀察期完成。若沒有穩定 hostname，必須重新建置/發布 App 或另立核准的 runtime-config 工作，不能臨時宣稱可純設定切換。

目前已安裝 App 沒有 per-install credential、membership 或家庭切換器，也可能仍以 `demo-household` 操作；切換前須升級到支援安全儲存、多家庭 onboarding/switcher 的版本，不能僅改 API URL 就視為完成身分遷移。沒有既有 email/password 帳戶可轉移。若遷移舊 device/membership registry，須一併驗證各家庭角色與 approval household 綁定；若撤銷舊 tokens，使用者須在每個家庭分別受邀，owner recovery 也只恢復單一家庭。若後端資料在 `demo-household`，先由資料擁有者決定是否保留為隔離的 legacy STG 資料或做明確 household mapping；不得自動與新建家庭合併。

## 9. 預算監控與資源清理

- 至少每日查看初期 Billing report；核對按 project、service、region 分組的 Cloud Run、Firestore、Cloud Storage、Artifact Registry、Secret Manager、網路費用及剩餘有效抵免額。用量資料可能延遲，勿只依賴報表或預算通知。
- 依剩餘抵免額設定保守預算和多段通知，但須知 Budget alert 不是硬上限。接近預設安全緩衝門檻，或無法確認未來用量仍由抵免額涵蓋時，凍結寫入、完成資料匯出驗證並停用/清理資源；不可升級付費帳戶來延長服務。
- 清理前確認目前 project、資源 owner、資料備份/保留義務、日誌與 rollback 狀態。撤除未用舊 revisions/images、測試 bucket/object、臨時匯出檔、未用 secrets 及不再需要的 IAM 綁定；刪除資料庫或 bucket 前需資料擁有者書面核准及獨立備份驗證。
- 不使用時降低或移除可計費資源，但注意刪除或停用服務可能影響資料存取、DNS及已安裝 App。記錄刪除項目與日期，保留必要稽核證據。

## 10. `nestocke-stg` 試用到期前匯出與清理

本節記錄目前私有 STG 資源，供到期前演練與人工執行；**本次未執行匯出、刪除、停用或帳務變更**。

- Project：`nestocke-stg`；Cloud Run：`nestock-stg-api`，`us-central1`。
- Firestore Native database：`(default)`，`us-central1`。
- GCS media/export bucket：`nestocke-stg-media-217316337914`，`US-CENTRAL1`；Uniform bucket-level access 已啟用、Public Access Prevention 為 `enforced`，且 bucket IAM 無公開成員。
- Cloud Run 使用 request-based 預設、min `0`、max `1`、CPU `1`、memory `1Gi`、concurrency `10`、timeout `300s`。映像停用 Uvicorn proxy-header trust；外部 staging URL 必須來自固定 HTTPS 設定，應用不得把 forwarded headers 當作授權或 rate-limit 身分。
- Cloud Run 維持 IAM 私有；不得為匯出/清理而加入公開 invoker。資料位置及資源名稱執行前須再次核對。

### 匯出前與驗證

先由資料擁有者書面核准資料範圍、保存目的地及保留期；確認有效 Welcome credit 足以涵蓋匯出與複製成本，並安排寫入凍結窗口。Budget 通知不是硬上限。不要把帳務帳戶 ID 或任何資料/憑證寫進命令歷史或日誌。

以下 PowerShell 命令建立唯一、可辨識的匯出 prefix，並同步等待 Firestore export 完成：

```powershell
$project = "nestocke-stg"
$database = "(default)"
$bucket = "nestocke-stg-media-217316337914"
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$exportUri = "gs://$bucket/wo010-export/$stamp"

gcloud config get-value project
gcloud firestore databases describe --database=$database --project=$project
gcloud storage buckets describe "gs://$bucket" --format="value(uniform_bucket_level_access,public_access_prevention)"
gcloud storage buckets get-iam-policy "gs://$bucket"
gcloud firestore export $exportUri --database=$database --project=$project
gcloud storage ls "$exportUri/**"
```

只有在 project/database/location、bucket 私有設定及 export operation 完成狀態均核對無誤時才繼續。記錄輸出檔數、總 bytes、operation 結果及執行時間；不可將文件內容或物件名貼入一般日誌。Firestore export 不等同完整服務備份，也不包含 Cloud Run 設定、IAM、Secrets、App image、GCS 媒體物件或其生命週期設定。GCS 媒體另依核准清單做 object 數量/bytes/checksum 核對。

同一 project/bucket 的 export 只是暫存副本。若要在試用關閉後保留資料，必須先取得資料擁有者核准，複製到**已存在且核准、私有、位置與成本已確認**的目的地，再比對物件數、大小與 checksum；不得為此自行建立付費資源或跨 project 複製家庭/PII 資料。未有核准目的地或無法驗證副本時，停止在匯出及保留決策，不得刪除原始資料。

### 到期清理（破壞性命令；只在獨立備份驗證及書面核准後執行）

先確認 project selector、資源 owner、稽核/保留義務及 rollback 窗口。以下命令僅供計畫執行，現在不得執行：

```powershell
gcloud run services delete nestock-stg-api --region=us-central1 --project=nestocke-stg
gcloud firestore databases delete --database="(default)" --project=nestocke-stg
gcloud storage rm --recursive "gs://nestocke-stg-media-217316337914"
gcloud artifacts repositories delete nestock-stg --location=us-central1 --project=nestocke-stg
```

刪除 Firestore database 或整個 GCS bucket 會永久移除資料；必須逐項由資料擁有者再次核准。Registry repository 可能包含多個 revisions/images，刪除前先盤點並保留稽核需要的 digest。Cloud Build staging bucket 可能供其他 builds 共用，只能在保留期結束後按精確 object 清理，禁止直接刪除共用 bucket。不要升級付費帳戶、解除/更換 billing link，或把 budget alert 當成自動停支上限。
