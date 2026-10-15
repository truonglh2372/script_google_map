# script_google_map — bộ lấy dữ liệu Google Maps cho Ăn Uống Quanh Đây

## Cấu trúc
- `common/` — dùng chung 2 máy: `aqd_api.py` (client API admin), `maps_fetch.py` (lấy thông tin quán, không cần trình duyệt), `safe_run.py` (chạy từng quán: list/run, đánh dấu `SAFE_DONE`), `photo_filter.py` (lọc ảnh bằng CLIP), `menu_worker.py` + `menu_ocr.py` + `menu_vocab.json` (OCR ảnh menu → tạo món/danh mục, đọc SĐT bảng hiệu).
- `windows/` — máy Admin: `install.ps1` (cài 1 lệnh), `alt.py` (vòng lặp fetch), `start_fetcher.ps1`, `start_menu_worker.ps1`, `aqd_*.cmd` (đặt trong thư mục Startup để tự chạy khi đăng nhập).
- `mac/` — Mac mini: script cài đặt; launchd plist (`aqd.alt`, `aqd.chrome`, `aqd.menu`) trong `mac/launchd/`.
- `tools/` — `cleanup.py` (sửa/ẩn món sai đã ghi), `build_vocab.py`, các patch.

## Cài trên máy Windows mới — 1 lệnh duy nhất

Dán **một dòng** vào PowerShell (repo public, không cần token):

```powershell
irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1 | iex
```

`install.ps1` tự làm hết: tải source từ GitHub (git clone, fallback zip `codeload`) → cài vào `C:\aqd` với **layout PHẲNG** (mọi `common/*.py` nằm **trực tiếp** trong `C:\aqd` vì `safe_run.py` làm `sys.path.insert(0, "C:/aqd")`) → thay hardcode `C:\Users\Admin\...\python.exe` bằng python thật của máy → cài dependency → **hỏi từng biến môi trường một prompt riêng** (`AQD_ADMIN_EMAIL`, `AQD_ADMIN_PASSWORD` nhập ẩn, `AQD_BASE`) và ghi `C:\aqd\creds.env` → **hỏi đuôi ID** và ghi `C:\aqd\aqd_config.json` → bật Chrome CDP 9222 với profile `C:\aqd\chrome-profile` → khởi động `alt.py`.

Chạy lại nhiều lần không phá gì: `creds.env`, `chrome-profile/` và `aqd_data/` được giữ, chỉ cập nhật source (hỏi trước khi ghi đè `creds.env`). Sau khi Chrome mở, **tự đăng nhập Google** trong cửa sổ đó — script không làm được.

Cờ khi cần (gọi qua script block, không cần biến môi trường): `-Root <dir>`, `-Clean`, `-NoStart`, `-NoChrome`, `-SkipHeavy`, `-Yes`.

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1))) -Root C:\aqd_test -NoStart -Yes
```

Cài vào chỗ khác khi test: đặt `$env:AQD_ROOT` trước khi `iex`.

### Dọn cài lại cho máy đã cài bản cũ (1 lệnh)

Máy đã có `C:\aqd` cũ (tiến trình `alt.py`/`safe_run.py` có thể đang chạy, `STOP_ALL`/`STOP`, `queue*.json`, `aqd_data/`, `chrome-profile/`, `creds.env`, `.aqd_session/`) thì dùng **chế độ dọn**, đúng thứ tự **dừng → báo cáo → sao lưu → xoá → cài lại**:

```powershell
$env:AQD_CLEAN=1; irm https://raw.githubusercontent.com/truonglh2372/script_google_map/main/windows/install.ps1 | iex
```

1. Tạo `STOP_ALL` + `STOP`, chờ tối đa 60s cho tiến trình tự thoát ở ranh giới quán; sau đó liệt kê tiến trình `python` còn lại và **hỏi trước khi** `Stop-Process -Force`.
2. **Báo cáo trước khi xoá**: số quán trong `aqd_data/`, `SAFE_DONE`, `SAFE_PREP`, số dòng `safe_run_log.md`, có `fetch_backoff.txt` không, dung lượng `chrome-profile/`.
3. **Sao lưu cứu hộ** `C:\aqd_backup_<timestamp>`: `creds.env`, `chrome-profile/`, `aqd_data/`. **`chrome-profile` chứa phiên đăng nhập Google — mất là phải đăng nhập lại từ đầu.**
4. Xoá trạng thái gây xung đột: `STOP*`, `queue*.json`, `parity_tries.json`, `fetch_backoff.txt`, `alt.out`, `safe_run_log.md`, `aqd_progress.md`, `aqd_api_log.md`, `start_fetcher.log`, `__pycache__/`, `.aqd_session/` (cache token cũ). **Giữ** `creds.env`, `chrome-profile/` (mặc định) và `aqd_data/`.
5. Cài lại source phẳng, vá python path cho máy đó, khôi phục/giữ `creds.env` + `chrome-profile`.
6. Bật lại như luồng thường.

## Chọn ID để chạy — theo ĐUÔI, không còn chẵn/lẻ

`alt.py` liệt kê toàn bộ quán nháp chưa có tên trên server rồi lọc phần của máy mình, chạy tăng dần; hết thì ngủ 10 phút rồi liệt kê lại.

**Quy tắc ưu tiên:**

1. **`AQD_ID_SUFFIXES`** (nếu khác rỗng) — lấy ID có **chữ số cuối** nằm trong danh sách, tức `id % 10 in suffixes`. Nhận cả `"1 3 5 7 9"` và `"1,3,5"`; khoảng trắng/dấu phẩy đều được, phần tử rỗng bị bỏ, chỉ nhận chữ số **0–9** (ngoài phạm vi bị báo lỗi rõ ràng, không im lặng). Ví dụ `1 3 5 7 9` → chỉ ID tận cùng 1, 3, 5, 7, 9; `2 4` → chỉ ID tận cùng 2 hoặc 4. Hai tập rời nhau nên không máy nào làm trùng việc của máy nào.
2. **Không có `AQD_ID_SUFFIXES`** (rỗng/không đặt) → quay về **`AQD_PARITY`** (`even`/`odd`, mặc định Windows = `even`, Mac = `odd`) **y như cũ**, để máy Mac đang chạy không vỡ.

Log dòng đầu của `alt.py` ghi rõ đang chọn gì: `id suffixes: 1,3,5,7,9 -> queue_ids_13579.json` (hoặc `parity loop start: even ids, ascending`).

**File queue** đặt tên theo cách chọn để nhiều máy trên cùng một máy tính không đè nhau: `queue_ids_13579.json` (các chữ số sắp tăng dần) khi dùng đuôi; `queue_even.json` / `queue_odd.json` khi dùng parity cũ.

- Cấu hình: `AQD_ROOT` (thư mục gốc, mặc định `C:/aqd`, mọi file trạng thái như `safe_run_log.md`, `aqd_data/`, `STOP` đều theo gốc này), `AQD_ID_SUFFIXES`, `AQD_PARITY`.
- `install.ps1` truyền `AQD_ID_SUFFIXES`/`AQD_PARITY` vào tiến trình `alt.py` **qua biến môi trường của tiến trình con** — không sửa `alt.py` khi cài.
- Dừng: tạo file `STOP_ALL` trong thư mục gốc (dừng sạch giữa 2 quán). `STOP_MENU` chỉ dừng menu worker.

## Lỗi tiếng Việt trên Windows (đã vá)

Windows mặc định **cp1252**, nên mọi `open()` thiếu `encoding="utf-8"` sẽ **`UnicodeEncodeError`** khi ghi tên/địa chỉ tiếng Việt (ví dụ `Hương Xưa Quán`) và làm `safe_run.py` chết ở bước ghi. Bản production không thấy lỗi vì `alt.py` export `PYTHONUTF8="1"` rồi chạy `safe_run.py` như subprocess; chạy trực tiếp là vỡ. Vì vậy **mọi builtin `open()` mở file text đều phải có `encoding="utf-8"`** (mode `wb`/`rb` giữ nguyên). `os.open()` và `Image.open()` (PIL) **không nhận** tham số `encoding` — với `os.open(...)` thì `encoding` đặt ở `os.fdopen(...)`.

## Bí mật
Token API nằm trong `creds.env` (KHÔNG commit). Không commit chrome-profile, cookie, dữ liệu `aqd_data/`, log, queue.
