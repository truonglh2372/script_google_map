# script_google_map — bộ lấy dữ liệu Google Maps cho Ăn Uống Quanh Đây

> **#855 (23/10/2026):** crawler **không còn model local** (bỏ CLIP, OCR, `photo_filter.py`, `menu_worker.py`). Mọi ảnh (trừ video) được upload lên API; API tự phân loại (food/drink/menu/interior/storefront_or_sign/receipt/other/unclassified) và chỉ ảnh menu mới đi vào flow đọc menu. `menu_worker` đã ngừng, không còn gọi `PUT /menu`, launchd `aqd.menu` để disabled.

## Cấu trúc
- `common/` — dùng chung 2 máy: `aqd_api.py` (client API admin), `maps_fetch.py` (lấy thông tin quán, không cần trình duyệt), `safe_run.py` (chạy từng quán: list/run, đánh dấu `SAFE_DONE`).
- `windows/` — máy Admin: `install.ps1` (cài 1 lệnh), `alt.py` (vòng lặp fetch), `start_fetcher.ps1`, `aqd_fetcher.cmd` (đặt trong thư mục Startup để tự chạy khi đăng nhập).
- `mac/` — Mac mini: `install.sh` (**cài 1 lệnh**, tương đương `windows/install.ps1`), `alt.py`, và launchd plist **mẫu** (`aqd.alt`, `aqd.chrome`) trong `mac/launchd/`; `install.sh` thay `__AQD_*__` theo root rồi ghi vào `~/Library/LaunchAgents/`. `install_mac.sh` **đã bị thay thế** (xem đầu file).
- `tools/` — `cleanup.py` (sửa/ẩn món sai đã ghi), test installer.

## ⚡ Cài / cập nhật bằng 1 lệnh, không hỏi gì (dùng cho máy đang chạy)

**Windows (Admin)**, dán vào PowerShell:
```powershell
$env:AQD_YES=1; irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1 | iex
```
**Mac mini**, dán vào Terminal:
```bash
curl -fsSL https://raw.githubusercontent.com/truonglh2372/script_google_map/main/mac/install.sh | AQD_YES=1 bash
```
Khi có `AQD_YES=1`, installer chạy hết **không hỏi gì**:
- Tải code mới nhất từ repo về.
- **Dừng sạch** fetcher và menu worker của root này: tạo `STOP_ALL`, chờ tối đa 10 phút để chúng dừng ở ranh giới giữa 2 quán. Quá 10 phút mới kill; done marker vẫn giữ tiến độ.
- Cài dependency nhẹ (`requirements.txt`). Không còn CLIP/easyocr.
- **Giữ nguyên** `creds.env`, `chrome-profile/`, `aqd_data/` (mọi `SAFE_DONE` / `MENU_DONE`) và cách chia ID đã lưu trong `aqd_config.json`. Nếu chưa lưu cách chia nào thì Windows lấy ID **chẵn**, Mac lấy ID **lẻ**.
- Đăng ký tự chạy khi đăng nhập: Startup folder trên Windows, launchd `aqd.chrome` / `aqd.alt` trên Mac (`aqd.menu` cũ bị bootout + disable).
- Xoá `STOP_ALL` rồi **bật lại** fetcher (`alt.py`).

Muốn đổi giá trị thì đặt biến môi trường cùng tên trước lệnh, ví dụ `AQD_ID_SUFFIXES="1 3"`, `AQD_PARITY=odd`, `AQD_ADMIN_EMAIL=...` hoặc `AQD_ADMIN_PASSWORD=...` (dùng khi máy mới chưa có `creds.env`). Bỏ `AQD_YES` thì installer quay về chế độ hỏi từng câu như mô tả bên dưới. Lưu ý: GitHub raw có thể trễ tới khoảng 5 phút sau khi push.

## Heartbeat cho báo cáo Slack hằng ngày (an_uong_quanh_day#1021)
`common/heartbeat.py` gửi `POST {AQD_BASE}/v1/ingest/heartbeats` để báo cáo Slack 08:00 biết máy này còn chạy, đã làm được bao nhiêu và gặp vấn đề gì.
- **Bật:** thêm một dòng `AQD_INGEST_TOKEN=<token>` vào `creds.env` (installer giữ nguyên `creds.env`). Token lấy ở Admin → Ingest clients (`/admin/ingest_clients`), **mỗi máy một token**; token chỉ hiện một lần, không bao giờ được in ra log. Không có token thì heartbeat không làm gì cả.
- **Gửi gì:** tên crawler `script_google_map`, tên máy (`CRAWLER_HOSTNAME` hoặc hostname), việc đang làm, số quán xong / ảnh đã đẩy / lỗi / lần bị giới hạn **kể từ heartbeat trước** (kèm `window_seconds`), chỗ trống ổ đĩa, trạng thái (`blocked` khi Maps chặn hoặc giới hạn, `error` khi server quá tải, `idle` khi nghỉ hoặc dừng) và ghi chú vấn đề. Không gửi mật khẩu, cookie hay URL có token.
- **Định dạng:** đúng hợp đồng của bảng `crawler_heartbeats` (web #1024) và endpoint web #1036 (`docs/runbooks/daily-ops-report.md`): `crawler`, `host`, `status` (`running` / `idle` / `blocked` / `error`), `activity`, `range_label`, `window_seconds`, `processed_count`, `photo_count`, `dish_count`, `error_count`, `rate_limited_count`, `disk_free_mb`, `note`. Trả về 201; 422 `invalid_heartbeat` hoặc 409 thì bỏ body đó, không gửi lại.
- **Khi nào:** sau mỗi quán (`safe_run.py`) và trong lúc `alt.py` nghỉ, nhưng tối đa 1 lần mỗi `AQD_HEARTBEAT_EVERY_MIN` phút (mặc định 15); gửi ngay khi bị chặn hoặc dừng.
- Số đếm cộng dồn trong `heartbeat_state.json`; gửi lỗi thì giữ lại và gửi lại đúng body + `Idempotency-Key` cũ, nên server không đếm hai lần. Lỗi heartbeat không bao giờ làm dừng crawler. `AQD_HEARTBEAT=0` để tắt.
- Test offline (không gọi mạng): `python3 tools/test_heartbeat.py`.

## Gọi thẳng server gốc, bỏ qua Cloudflare (an_uong_quanh_day#1058)
Đường Viettel → Cloudflare SG hay mất gói nên gọi API qua Cloudflare chậm. Thêm **một dòng** vào `creds.env` (hoặc biến môi trường) để gọi thẳng server gốc:

```
AQD_ORIGIN_IP=<IP server gốc, chủ dự án đưa>
```

- Giống `curl --resolve`: chỉ việc tra DNS của host trong `AQD_BASE` được thay bằng IP đó. URL, SNI, header `Host` và việc kiểm tra chứng chỉ vẫn dùng tên `anuongquanhday.com`, nên chứng chỉ phải khớp tên (server gốc có sẵn chứng chỉ Let's Encrypt cho tên này). **Không bao giờ tắt kiểm tra TLS**; token / đăng nhập admin không đổi.
- Chỉ pin đúng host của `AQD_BASE`; Google, Foody, CDN ảnh... vẫn tra DNS bình thường.
- `AQD_BASE` phải là `https://...` và `AQD_ORIGIN_IP` phải là đúng một địa chỉ IP, không thì client admin báo lỗi ngay (`OriginPinError`) còn heartbeat tự tắt. Để trống / không có dòng này = như cũ (qua Cloudflare).
- **Bỏ:** xoá dòng đó rồi khởi động lại crawler. Chạy lại installer và chọn ghi đè `creds.env` sẽ mất dòng này (khi đó crawler lại đi qua Cloudflare, vẫn chạy được); thêm lại sau khi cài.
- Kiểm tra trên máy: `curl -s -o /dev/null -w '%{http_code} %{time_total}\n' --resolve anuongquanhday.com:443:<IP> https://anuongquanhday.com/up` phải ra `200`.
- Test offline: `python3 tools/test_origin_pin.py` (server TLS thật ở 127.0.0.1 với CA tạm; phần `requests` cần `pip install -r requirements.txt`).

## Dữ liệu Google thêm (từ 17/10/2026)
Khi fetch một quán, `maps_fetch.py` lấy thêm các dữ liệu dưới đây, **không tốn thêm request nào tới Google** (đọc ngay từ JSON preview vốn đã tải):
- **Đánh giá:** `google_rating`, `google_review_count`, phân bố 1–5 sao, và tối đa **20** đánh giá có sẵn trong preview. Mỗi đánh giá gồm tên hiển thị của người viết (chỉ tên, không lưu id hay avatar), số sao, nội dung, ngày đăng và ảnh kèm theo. Ảnh chỉ được giữ khi CLIP xếp là `food` / `drink`, tối đa 12 ảnh mỗi quán (`AQD_REVIEW_PHOTO_CAP`) và chỉ lưu URL.
- **Giờ mở cửa theo từng ngày:** `opening_periods` dạng `{weekday 1=T2..7=CN, opens_at, closes_at}`. Hỗ trợ ca gãy (2 dòng một ngày), ngày nghỉ (không có dòng nào) và mở 24 giờ (`00:00` với `closes_at=null`).
- **Giới thiệu (About):** các nhóm thuộc tính (dịch vụ, đỗ xe, thanh toán, tiện nghi, hỗ trợ người khuyết tật…) và mô tả quán.

Dữ liệu được ghi đi đâu:
- Server: chỉ **giờ mở cửa**, qua `PUT /api/v1/admin/restaurants/:id/opening_periods`, và **chỉ khi quán chưa có giờ nào**. Giờ do chủ quán hay admin đã nhập không bao giờ bị ghi đè. `opening_hours_text` vẫn được ghi như cũ.
- Máy local: mọi thứ (cả phần server chưa có chỗ chứa) nằm ở `aqd_data/<id>/extra.json`. Hai issue cho phần server còn thiếu: truonglh2372/an_uong_quanh_day#680 (đánh giá) và #679 (About + description).
- `tools/extra_backfill.py [--dry] id...` chạy lại phần này cho những quán đã fetch trước đó.

## Cài trên máy Windows mới — 1 lệnh duy nhất

Dán **một dòng** vào PowerShell (repo public, không cần token):

```powershell
irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1 | iex
```

`install.ps1` tự làm hết: tải source từ GitHub (git clone, fallback zip `codeload`) → cài vào `C:\aqd` với **layout PHẲNG** (mọi `common/*.py` nằm **trực tiếp** trong `C:\aqd` vì `safe_run.py` làm `sys.path.insert(0, "C:/aqd")`) → dọn `*.py` lạ ở gốc root không thuộc payload mới (có sao lưu, xem dưới) → tìm python thật của máy (**ưu tiên 3.12**, cảnh báo nếu máy chỉ có 3.13+) → thay hardcode `C:\Users\Admin\...\python.exe` bằng python đó → cài dependency → **hỏi từng biến môi trường một prompt riêng** (`AQD_ADMIN_EMAIL` **bắt buộc**, `AQD_ADMIN_PASSWORD` nhập ẩn — Enter giữ giá trị cũ trong `creds.env` nếu có, `AQD_BASE` mặc định `https://anuongquanhday.com`) và ghi `C:\aqd\creds.env` → **hỏi đuôi ID** (**bắt buộc**, xem mục chia việc), `AQD_LIST_MAX_PAGES` và `AQD_FETCH_LIMIT` (**Enter là xong**) và ghi `C:\aqd\aqd_config.json` → bật Chrome CDP (cổng riêng cho root này, profile `C:\aqd\chrome-profile`) → khởi động `alt.py`.

### Mọi prompt đều có mặc định — Enter là xong

Installer in ngay đầu một dòng tóm tắt để biết Enter sẽ ra gì:

```
Enter = defaults: base=https://anuongquanhday.com  id_suffixes=<required>  list_max_pages=20  fetch_limit=0  startup=no
REQUIRED (no default, asked until valid): AQD_ID_SUFFIXES (digits 0-9), AQD_ADMIN_EMAIL. Everything else is Enter.
AQD_ADMIN_PASSWORD: Enter keeps the value already in creds.env when there is one.
```

- **Mọi prompt đều có mặc định**: Enter dùng luôn mặc định, không phải gõ.
- Chỉ **2 trường bắt buộc** (không thể mặc định được): `AQD_ID_SUFFIXES` (chữ số 0-9; nhập sai/để trống thì hỏi lại) và `AQD_ADMIN_EMAIL`. `AQD_ADMIN_PASSWORD`: Enter giữ mật khẩu cũ trong `creds.env` (nếu đã cài).
- Khi **stdin bị redirect** (chạy tự động, không có console thật): installer **tự in câu hỏi ra stdout** trước khi đọc, và ghi chú rằng mật khẩu sẽ **hiện chữ** vì không có console thật (trên console thật mật khẩu vẫn ẩn).

Chạy lại nhiều lần không phá gì: `creds.env`, `chrome-profile/` và `aqd_data/` được giữ, chỉ cập nhật source (hỏi trước khi ghi đè `creds.env`). Sau khi Chrome mở, **tự đăng nhập Google** trong cửa sổ đó — script không làm được.

### Lần chạy đầu mất bao lâu

Bấm xong tới lúc cào quán đầu tiên gồm 2 phần: **chờ Chrome** (vài chục giây) và **liệt kê ID từ server** (`1 request / 3 giây`). Bản cũ list **toàn bộ** rồi mới cào → 239 trang ≈ **12 phút** đứng im. Từ bản này `alt.py` chỉ list **`AQD_LIST_MAX_PAGES` trang mỗi vòng** (mặc định **20** ≈ **1 phút**), in ETA trước khi list rồi cào luôn; các vòng sau list tiếp phần còn lại (trang kế tiếp, hết thì quay lại đầu danh sách) nên **không sót quán nào**.

Dòng log trong `alt.out` (và `safe_run_log.md`) trước khi list:

```
17:30:15 listing pages 1..20 of unnamed 1,3,5,7,9 drafts (20 pages this round, ~1 min of listing) - fetching starts right after
17:30:16 LIST mode=paged per_page=50 start_page=1 max_pages=20 id_suffix=1,3,5
17:30:16 listing 241 pages (~12 min at 3s/page) - listing only the first 20/241 pages now (~1 min), fetching starts right after
17:31:20 FETCH 380 1,3,5,7,9 drafts #12878..#18916
```

`AQD_LIST_MAX_PAGES=0` (hoặc rỗng) = hành vi cũ: list hết rồi mới cào. `AQD_FETCH_LIMIT=N` = mỗi vòng chỉ cào tối đa N quán rồi list lại (mặc định `0` = không giới hạn) — máy yếu/test dùng `10`.

Khi backend đã có `per_page` / `unnamed` / `id_suffix` / `summary` thì `alt.py` xin `per_page=50` + `unnamed=1` + `id_suffix` để mỗi trang chỉ chứa quán của máy mình; nếu server **chưa** có (hoặc bỏ qua `per_page`), script tự phát hiện và **quay về 24/trang như cũ** — log ghi `LIST mode=legacy …`, không crash, không sót quán.

### Gói nặng

Không còn (#855): ảnh do API phân loại. `-SkipHeavy` / `--skip-heavy` vẫn nhận nhưng không còn tác dụng.

Cờ khi cần (gọi qua script block, không cần biến môi trường): `-Root <dir>`, `-Clean`, `-NoStart`, `-NoChrome`, `-SkipHeavy`, `-Yes`.

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1))) -Root C:\aqd_test -NoStart -Yes
```

Cài vào chỗ khác khi test: đặt `$env:AQD_ROOT` trước khi `iex`. Cài payload của nhánh/tag khác (test trước khi merge): đặt `$env:AQD_REPO_REF` (mặc định `main`).

### Dọn cài lại cho máy đã cài bản cũ (1 lệnh)

Máy đã có `C:\aqd` cũ (tiến trình `alt.py`/`safe_run.py` có thể đang chạy, `STOP_ALL`/`STOP`, `queue*.json`, `aqd_data/`, `chrome-profile/`, `creds.env`, `.aqd_session/`) thì dùng **chế độ dọn**, đúng thứ tự **dừng → báo cáo → sao lưu → xoá → cài lại**:

```powershell
$env:AQD_CLEAN=1; irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1 | iex
```

1. Tạo `STOP_ALL` + `STOP` **trong đúng root đang cài**, chờ tối đa 60s cho tiến trình **của root đó** tự thoát ở ranh giới quán; sau đó liệt kê tiến trình `python` **còn lại của root đó** và **hỏi trước khi** `Stop-Process -Force`. Tiến trình `alt.py`/`safe_run.py` thuộc **root khác** chỉ được **cảnh báo, KHÔNG kill** (máy có thể đang chạy bản cài ở root khác).
2. **Báo cáo trước khi xoá**: số quán trong `aqd_data/`, `SAFE_DONE`, `SAFE_PREP`, số dòng `safe_run_log.md`, có `fetch_backoff.txt` không, dung lượng `chrome-profile/`.
3. **Sao lưu cứu hộ** `C:\aqd_backup_<timestamp>`: `creds.env`, `chrome-profile/`, `aqd_data/`. **`chrome-profile` chứa phiên đăng nhập Google — mất là phải đăng nhập lại từ đầu.**
4. Xoá trạng thái gây xung đột: `STOP*`, `queue*.json`, `parity_tries.json`, `fetch_backoff.txt`, `alt.out`, `safe_run_log.md`, `aqd_progress.md`, `aqd_api_log.md`, `start_fetcher.log`, `__pycache__/`, `.aqd_session/` (cache token cũ). **Giữ** `creds.env`, `chrome-profile/` (mặc định) và `aqd_data/`.
5. Cài lại source phẳng, vá python path cho máy đó, khôi phục/giữ `creds.env` + `chrome-profile`.
6. **Dọn `*.py` lạ ở gốc root** (không thuộc payload mới) trước khi copy, để module cũ không `import` đè — **liệt kê** danh sách sắp xoá, **sao lưu** vào `...\stale_py\`, và **KHÔNG** đụng `creds.env`, `chrome-profile/`, `aqd_data/`, `aqd_config.json`.
7. Bật lại như luồng thường.

### Chrome luôn theo root (không nhiễm chéo profile)

Mỗi root có **cổng CDP riêng** (mặc định `9222`, ghi trong `aqd_config.json` → `cdp_port`) và **profile riêng** `<root>\chrome-profile`. Khi installer/launcher thấy cổng CDP đã mở, nó đọc `--user-data-dir` của tiến trình Chrome đó:

- Trỏ đúng `<root>\chrome-profile` của mình → **dùng lại**.
- Trỏ **root khác** → **KHÔNG dùng lại**; cảnh báo rõ và mở Chrome ở **cổng kế tiếp còn trống**. Cài vào root mới **không bao giờ** ghi vào profile của root cũ.

Cùng cơ chế theo root cho **mutex single-instance** (`Global\aqd_fetcher_launcher_<root>`) và file `STOP_ALL`.

## Cài trên Mac mini — 1 lệnh duy nhất

Dán **một dòng** vào Terminal (repo public, không cần token):

```bash
curl -fsSL https://raw.githubusercontent.com/truonglh2372/script_google_map/main/mac/install.sh | bash
```

`mac/install.sh` là bản tương đương `windows/install.ps1`: tải source từ GitHub (`git clone`, fallback zip `codeload`) → cài vào `$HOME/aqd` với **layout PHẲNG** (mọi `common/*.py` + `common/menu_vocab.json` nằm **trực tiếp** trong `$R/` vì `safe_run.py` làm `sys.path.insert(0, AQD_ROOT)`; thêm `mac/alt.py` → `$R/alt.py`; riêng trên macOS `common/photos_new.py` được cài **thành** `$R/photos.py` vì `common/photos.py` hardcode `C:/aqd`) → dọn `.py` lạ ở gốc root (**có sao lưu, có liệt kê**, không đụng `creds.env` / `chrome-profile` / `aqd_data` / `aqd_config.json`) → dùng lại `$R/venv` nếu có, không thì tạo mới → hỏi từng biến môi trường một prompt riêng → ghi `creds.env` + `aqd_config.json` → mở Chrome CDP **theo root** → chạy `alt.py` (hoặc đăng ký launchd).

Chạy thử **không đổi gì trên máy** (khuyến nghị, nhất là Mac đang chạy bản vá tay):

```bash
curl -fsSL https://raw.githubusercontent.com/truonglh2372/script_google_map/main/mac/install.sh | AQD_DRY_RUN=1 bash
```

Đổi chỗ cài: đặt `AQD_ROOT="$HOME/aqd_test"` trước `curl`. Cài payload của nhánh/tag khác khi test: `AQD_REPO_REF=<nhánh>`. Cài từ checkout local, không cần mạng: `AQD_SRC_DIR=/duong/dan bash mac/install.sh`. Chạy như file: `bash mac/install.sh --root DIR --clean --no-start --no-chrome --skip-heavy --yes --dry-run`.

Câu trả lời được đọc từ `/dev/tty`, nên `curl ... | bash` vẫn hỏi bình thường (không ăn mất dòng script).

### Mọi prompt đều có mặc định — Enter là xong

Installer in ngay đầu một dòng tóm tắt để biết Enter sẽ ra gì:

```
Enter = defaults: base=https://anuongquanhday.com  id_suffixes=<required>  list_max_pages=20  fetch_limit=0  startup=no
REQUIRED (no default, asked until valid): AQD_ID_SUFFIXES (digits 0-9), AQD_ADMIN_EMAIL. Everything else is Enter.
AQD_ADMIN_PASSWORD: Enter keeps the value already in creds.env when there is one.
```

| Prompt | Enter = | Bắt buộc |
|---|---|---|
| `AQD_ADMIN_EMAIL` | — (không có mặc định) | **BẮT BUỘC**; phải có `@`, sai thì hỏi lại |
| `AQD_ADMIN_PASSWORD` | giữ giá trị đang có trong `creds.env` (chưa có file thì **bắt buộc**) | có điều kiện |
| `AQD_BASE` | `https://anuongquanhday.com` | không |
| `AQD_ID_SUFFIXES` | — (không có mặc định) | **BẮT BUỘC**; chỉ chữ số 0-9, nhận `"1 3 5"` và `"1,3,5"`; `even`/`odd` quay về `AQD_PARITY`; sai thì hỏi lại |
| `AQD_LIST_MAX_PAGES` | `20` | không |
| `AQD_FETCH_LIMIT` | `0` | không |
| Đăng ký auto-start (launchd) | **No**, trừ khi root là mặc định (`$HOME/aqd`) | không |

Giống Windows: **cảnh báo trùng đuôi/parity** (`suffix 9 is INSIDE parity=odd (1,3,5,7,9) - ...`) và khi đã có đuôi thì ghi `AQD_PARITY=''` để hết mơ hồ.

### An toàn với bản vá tay đang chạy trên Mac

Mac hiện chạy code khác repo (`alt.py` lấy từ `/tmp/alt_mac.py`), nên trước khi ghi đè installer **luôn**:

1. **Sao lưu + in dấu vân tay** `alt.py` và `safe_run.py` hiện có: dòng `existing alt.py: sha256=<hex> lines=<n>` cho từng file, bản sao ở `~/aqd_backup_<timestamp>/preinstall_py/`.
2. Nếu `alt.py` trên Mac **khác** `mac/alt.py` của repo → **cảnh báo rõ** `this Mac's alt.py DIFFERS from the repo's mac/alt.py - installing the repo version OVERWRITES a different build`, in **cả hai** sha256 + số dòng, in lệnh `diff -u` để tự xem, và **hỏi xác nhận — mặc định `No`**. Chọn No/Enter → installer **dừng** (exit 3), **không ghi gì**, và nói rõ backup ở đâu. Ai đang chạy không có console (stdin không phải terminal) thì mặc định cũng là No → dừng an toàn.
3. Nếu thấy `queue6.json` / `queue8.json` / `alt_step.txt` → ghi nhận **«thế hệ cũ»**, sao lưu vào `~/aqd_backup_<timestamp>/old_queue/` rồi xoá để không tranh chấp với `queue_all_unnamed.json` / `queue_ids_*.json` của bản mới.

### launchd plist theo root (không hardcode user)

`mac/launchd/*.plist` giờ là **mẫu** (có `__AQD_ROOT__`, `__AQD_PY__`, `__AQD_ID_SUFFIXES__`, …), **không** còn `/Users/hoangtruongle`. Khi đăng ký auto-start, `install.sh` render theo root và ghi vào `~/Library/LaunchAgents/aqd.{alt,chrome,menu}.plist` (bản render cũng để ở `$R/launchd/`). `aqd.alt.plist` **thêm `EnvironmentVariables`**: `AQD_ROOT`, `AQD_ID_SUFFIXES`, `AQD_PARITY`, `AQD_LIST_MAX_PAGES`, `AQD_FETCH_LIMIT`, `AQD_CDP_PORT` (trước đây **không có chỗ bơm**, nên phiên đăng nhập chạy sai cấu hình). `launchctl bootout` chỉ chạy khi plist đang có trỏ về **root này**; plist của root khác chỉ bị cảnh báo, không đụng. `alt.py` cũng đọc `aqd_config.json` làm fallback cho `cdp_port` / `list_max_pages` / `fetch_limit`.

### Dọn và cài lại (1 lệnh)

Máy đã cài bản cũ (tiến trình đang chạy, `STOP_ALL`/`STOP`, `queue*.json`, `aqd_data/`, `chrome-profile/`, `creds.env`) thì dùng **chế độ dọn**, đúng thứ tự **dừng → báo cáo → sao lưu → xoá → cài lại**:

```bash
curl -fsSL https://raw.githubusercontent.com/truonglh2372/script_google_map/main/mac/install.sh | AQD_CLEAN=1 bash
```

1. Tạo `STOP_ALL` + `STOP` trong **đúng root đang cài**, chờ tối đa 60s cho tiến trình **của root đó** tự thoát; sau đó liệt kê tiến trình còn lại và **hỏi trước khi** kill (`Kill -9 the N process(es) under <root> (and only those)?`, mặc định **No**). Tiến trình `alt.py`/`safe_run.py`/`menu_worker.py` thuộc **root khác** chỉ được **cảnh báo, KHÔNG kill**.
2. **Báo cáo trước khi xoá**: số quán trong `aqd_data/`, `SAFE_DONE`, `SAFE_PREP`, số dòng `safe_run_log.md`, có `fetch_backoff.txt` không, dung lượng `chrome-profile/`, số `queue*.json`.
3. **Sao lưu cứu hộ** `~/aqd_backup_<timestamp>`: `creds.env`, `chrome-profile/`, `aqd_data/` (mặc định **Yes**). `chrome-profile` chứa phiên đăng nhập Google — mất là phải đăng nhập lại.
4. Xoá trạng thái gây xung đột: `STOP*`, `queue*.json`, `parity_tries.json`, `fetch_backoff.txt`, `alt.out`, `alt.nohup`, `safe_run_log.md`, `aqd_progress.md`, `aqd_api_log.md`, `start_fetcher.log`, `menu_worker.*`, `photos_backoff.txt`, `__pycache__/`, `.aqd_session/`. **Giữ** `creds.env`, `chrome-profile/`, `aqd_data/`, `aqd_config.json`.
5. Cài lại source phẳng (dọn `.py` lạ có sao lưu), dùng lại `venv`, giữ `creds.env`.
6. In ở cuối: lệnh xem log (`tail -f $R/alt.out`), lệnh dừng (`touch $R/STOP_ALL`), và lệnh chạy lại.

`AQD_DRY_RUN=1` in **mọi hành động sẽ làm** (mọi dòng có tiền tố `[dry-run]`) mà không thực thi gì trên máy — chỉ đọc (tải source vào thư mục tạm để so sánh `alt.py`), nên chủ dự án kiểm được toàn bộ kế hoạch trước khi chạy thật.

### Chưa kiểm chứng được (không có máy Mac)

Máy phát triển là Windows: **không có** `launchctl`, `caffeinate`, `/Applications/Google Chrome.app`, `plutil`. Vì vậy:

- **Đã chạy**: `bash -n mac/install.sh` (cú pháp), harness mô phỏng `tools/test_installer_mac.sh` (stub `launchctl`/`pgrep`/`pkill`/`curl`/`caffeinate`/`open`/`git`/`python3`/`sha256sum`, chạy **toàn bộ luồng** cài mới + chế độ dọn), `python tools/test_id_filter.py`, `python tools/test_list_paging.py`, và 2 test installer Windows (không đổi).
- **CHƯA chạy được**: cài thật rồi `launchctl bootstrap`/`caffeinate`/Chrome thật trên macOS; `plutil -lint` plist đã render; `shellcheck` (không có trên máy này). Cần chủ dự án chạy `AQD_DRY_RUN=1` trước trên Mac, rồi cài thật và để ý bước 8 (Chrome/launchd) và `~/Library/LaunchAgents/aqd.alt.plist`.

## Chọn ID để chạy — theo ĐUÔI, không còn chẵn/lẻ

`alt.py` liệt kê toàn bộ quán nháp chưa có tên trên server rồi lọc phần của máy mình, chạy tăng dần; hết thì ngủ 10 phút rồi liệt kê lại.

**Quy tắc ưu tiên:**

1. **`AQD_ID_SUFFIXES`** (nếu khác rỗng) — lấy ID có **chữ số cuối** nằm trong danh sách, tức `id % 10 in suffixes`. Nhận cả `"1 3 5 7 9"` và `"1,3,5"`; khoảng trắng/dấu phẩy đều được, phần tử rỗng bị bỏ, chỉ nhận chữ số **0–9** (ngoài phạm vi bị báo lỗi rõ ràng, không im lặng). Ví dụ `1 3 5 7 9` → chỉ ID tận cùng 1, 3, 5, 7, 9; `2 4` → chỉ ID tận cùng 2 hoặc 4. **Đuôi thắng parity khi có**: installer ghi `AQD_PARITY=''` để không mơ hồ.
2. **Không có `AQD_ID_SUFFIXES`** (rỗng/không đặt) → quay về **`AQD_PARITY`** (`even`/`odd`, mặc định Windows = `even`, Mac = `odd`) **y như cũ**, để máy Mac đang chạy không vỡ.

### Chia việc giữa các máy (QUAN TRỌNG)

**Đuôi ID là duy nhất toàn hệ.** Nếu dùng đuôi thì các máy phải **rời nhau** (không giao); hai máy cùng một đuôi sẽ **cào trùng** đúng những ID đó.

- Chia đôi đúng kiểu đơn giản nhất: **một bên chẵn `0 2 4 6 8`, một bên lẻ `1 3 5 7 9`**.
- Hoặc chia lệch: Máy A `1 3 5 7`, Máy B `9` + phần còn lại (`0 2 4 6 8`).
- ⚠ **`AQD_PARITY=odd` (mặc định của Mac) tương đương `1 3 5 7 9`**, và `AQD_PARITY=even` tương đương `0 2 4 6 8`. Nên một máy để `AQD_PARITY=odd` sẽ **trùng với bất kỳ máy nào dùng đuôi lẻ**. Khi dùng đuôi, tốt nhất **tắt parity** trên máy kia (`AQD_PARITY=`).

Installer **cảnh báo ngay khi cài** nếu đuôi giao với một parity, ví dụ đặt `9`:

```
[!] suffix 9 is INSIDE parity=odd (1,3,5,7,9) - if another machine runs parity=odd you will both fetch id%10=9. Set the other machine to a disjoint set.
```

Log dòng đầu của `alt.py` ghi rõ đang chọn gì: `id suffixes: 1,3,5,7,9 -> queue_ids_13579.json` (hoặc `parity loop start: even ids, ascending`).

**File queue** đặt tên theo cách chọn để nhiều máy trên cùng một máy tính không đè nhau: `queue_ids_13579.json` (các chữ số sắp tăng dần) khi dùng đuôi; `queue_even.json` / `queue_odd.json` khi dùng parity cũ.

- Cấu hình: `AQD_ROOT` (thư mục gốc, mặc định `C:/aqd`, mọi file trạng thái như `safe_run_log.md`, `aqd_data/`, `STOP` đều theo gốc này), `AQD_ID_SUFFIXES`, `AQD_PARITY` (installer ghi rỗng khi đã đặt đuôi), `AQD_LIST_MAX_PAGES` (số trang list mỗi vòng, mặc định 20; `0`/rỗng = list hết), `AQD_FETCH_LIMIT` (số quán cào mỗi vòng, mặc định 0 = hết), `AQD_CDP_PORT` (cổng Chrome CDP, mặc định 9222, ghi trong `aqd_config.json` → `cdp_port`).
- Trạng thái list từng phần nằm ở `queue*.json.state.json` (ví dụ `queue_ids_13579.json.state.json`): `start_page` là trang sẽ list ở vòng sau. Xoá file này thì vòng sau list lại từ đầu danh sách.
- `install.ps1` truyền `AQD_ID_SUFFIXES`/`AQD_PARITY`/`AQD_LIST_MAX_PAGES`/`AQD_FETCH_LIMIT` vào tiến trình `alt.py` **qua biến môi trường của tiến trình con** — không sửa `alt.py` khi cài.
- Dừng: tạo file `STOP_ALL` trong thư mục gốc (dừng sạch giữa 2 quán). `STOP_MENU` chỉ dừng menu worker.

## Lỗi tiếng Việt trên Windows (đã vá)

Windows mặc định **cp1252**, nên mọi `open()` thiếu `encoding="utf-8"` sẽ **`UnicodeEncodeError`** khi ghi tên/địa chỉ tiếng Việt (ví dụ `Hương Xưa Quán`) và làm `safe_run.py` chết ở bước ghi. Bản production không thấy lỗi vì `alt.py` export `PYTHONUTF8="1"` rồi chạy `safe_run.py` như subprocess; chạy trực tiếp là vỡ. Vì vậy **mọi builtin `open()` mở file text đều phải có `encoding="utf-8"`** (mode `wb`/`rb` giữ nguyên). `os.open()` và `Image.open()` (PIL) **không nhận** tham số `encoding` — với `os.open(...)` thì `encoding` đặt ở `os.fdopen(...)`.

### Kiểm thử offline (không cần mạng, không tải GB)

```powershell
python tools/test_id_filter.py          # lọc ID theo đuôi + parity (cả windows/alt.py và mac/alt.py)
python tools/test_list_paging.py        # list từng phần: ETA, cửa sổ trang, AQD_FETCH_LIMIT, fallback server cũ
python tools/test_audit_gaps.py         # #1083 review 50 + phân trang, #1084 ảnh gửi 0 + replay, #1085 ổ đĩa, #1087 thin
python tools/test_no_streetview.py      # #681: không tải ảnh Street View lên, không đặt làm ảnh bìa
python tools/test_origin_pin.py         # an_uong_quanh_day#1058: AQD_ORIGIN_IP gọi thẳng server gốc, TLS vẫn kiểm tra theo tên
powershell -File tools/test_installer_stream.ps1   # Nat -Stream (tiến độ pip) + tín hiệu sống + timeout
powershell -File tools/test_installer_guards.ps1   # guard theo root, Chrome theo root, cảnh báo đuôi/parity, CLEAN dọn .py lạ, mọi prompt có mặc định
bash tools/test_installer_mac.sh        # installer Mac: prompt/mặc định, layout phẳng, aqd_config.json, cảnh báo trùng, guard root, Chrome theo root, plist, CLEAN (mọi công cụ macOS đều được stub; cần bash)
```

`tools/test_list_paging.py` dựng một server giả ở `127.0.0.1` (cả 2 chế độ: có và không có `per_page`) và chạy
`safe_run.py list` thật; `tools/test_installer_stream.ps1` chạy một tiến trình con in dần để chứng minh output
được stream và dòng "still running" xuất hiện — không tải package nào.

## Sửa lỗi sau audit 24/10/2026 (an_uong_quanh_day#1083 #1084 #1085 #1087)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `AQD_MAX_REVIEWS` | `50` | Số review Google giữ mỗi quán (#1083). Preview chỉ có 5–8, phần còn lại phân trang `listugcposts` **trong Chrome đã đăng nhập** (chưa đăng nhập Google trả 403 → giữ review của preview). |
| `AQD_GOOGLE_REVIEWS_PUSH_MAX` | `= AQD_MAX_REVIEWS` | Số review gửi lên `PUT google_reviews`. Server cũ còn giới hạn 20 (`validate.rb`): gặp 422 thì gửi lại 20 review mới nhất và nhớ giới hạn 20 cho tới hết tiến trình — không hỏng gì trước khi deploy web. |
| `AQD_PHOTO_RETRY_BACKOFF_MIN` | `15` | #1084: quán **có ảnh được chọn nhưng server nhận 0 ảnh** không còn là OK/SAFE_DONE mà là `ERROR PHOTOS_UNSENT`, vẫn draft, ghi `photo_retry.json`; thử lại sau 15, 30, 60… phút (tối đa 6 giờ). |
| `AQD_PHOTO_RETRY_MAX` | `3` | Sau 3 lần thử lại vẫn 0 ảnh → lần cuối hoàn tất quán không ảnh (`WARN photos_unsent_gave_up`, entry `gave_up`). `alt.py` gọi `safe_run.py push --photo-retry` khi có quán đến hạn. |
| `AQD_MIN_FREE_GB` | `5` | #1085: ổ dữ liệu còn trống < 5 GB → không cào/đẩy; `alt.py` ghi log + heartbeat `disk_full`/`blocked` rồi nghỉ; `safe_run` chạy dưới `alt.py` thoát mã `4`. `0` = tắt. |
| `AQD_DISK_PAUSE_MIN` | `10` | Thời gian nghỉ mỗi lần ổ đầy trước khi kiểm tra lại. |
| `AQD_ALLOW_THIN` | `0` | #1087: **dữ liệu Maps "thin" không bao giờ được validate.** `run` (chế độ CDP) kiểm tra Chrome đã đăng nhập Google (chỉ xem *tên* cookie `SID`/`__Secure-1PSID`/`__Secure-3PSID`, không đọc giá trị); chưa đăng nhập → `REFUSE TO RUN`, mã thoát `5`, `alt.py` chờ 30 phút. Quán trả về thin → không gửi gì, không `info.json`/`SAFE_PREP`, ghi `THIN` + `info.thin.json`, quán vẫn là draft chưa tên nên vòng sau cào lại; 3 quán thin liên tiếp → nghỉ Maps như trang rỗng. `push` không bao giờ validate `info.json` thin. `1` = hành vi cũ (chỉ cho test/chạy tay). |

### Gửi lại ảnh cho quán "chọn ảnh nhưng gửi 0" (#1084) — CHUẨN BỊ, CHƯA CHẠY

Danh sách ID (chỉ đọc `aqd_data`, tạo 24/10/2026 21:00 ICT): `tools/replay_ids/gmaps_<máy>_{7d,all}.tsv` (`id  số ảnh đã chọn  SAFE_DONE  thin`).

| File | Số quán | Ghi chú |
|---|---|---|
| `gmaps_mac_7d.tsv` | 5 | SAFE_DONE trong 7 ngày (mới nhất 18/10 08:36) |
| `gmaps_admin_7d.tsv` | 405 | mới nhất 18/10 20:19 |
| `gmaps_mac_all.tsv` | 2617 | 27 dòng `thin` |
| `gmaps_admin_all.tsv` | 3429 | 74 dòng `thin` |

Tạo lại (chỉ đọc): `python tools/replay_ids/make_ids.py <AQD_ROOT> tools/replay_ids <mac|admin>`. Tiêu chí: `info.json` có `photo_urls`, `applied.json` không có `photo_id` nào, đã `SAFE_DONE`. Từ 18/10 20:19 không còn quán mới nào như vậy.
Phần lớn danh sách `all` là quán trước #704 (≤ 18/10 08:36): khi đó POST ảnh chạy async (202, `photos: []`) nên `applied.json` không có id dù server **có** ảnh — vì vậy `--apply` luôn GET ảnh trên server trước và bỏ qua quán đã có ảnh.

```bash
python safe_run.py replay-photos --ids-file tools/replay_ids/gmaps_mac_7d.tsv                 # dry run: chỉ đọc file local, không request
python safe_run.py replay-photos --ids-file tools/replay_ids/gmaps_mac_7d.tsv --check-server  # chỉ GET: bao nhiêu quán thật sự chưa có ảnh
python safe_run.py replay-photos --ids-file tools/replay_ids/gmaps_mac_7d.tsv --apply --limit 20   # gửi thật (cần được duyệt)
```
`--apply` chỉ POST ảnh (chunk 5, Guard 3 s, STOP file, kiểm tra ổ đĩa), không PATCH info/status, không ghi `SAFE_DONE`; quán đã xử lý ghi vào `replay_photos.done` nên chạy lại sẽ tiếp tục. Dừng crawler (`STOP_ALL`) trước khi chạy để không tranh `applied.json`.

## Bí mật
Token API nằm trong `creds.env` (KHÔNG commit). Không commit chrome-profile, cookie, dữ liệu `aqd_data/`, log, queue.
