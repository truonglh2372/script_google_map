# script_google_map — bộ lấy dữ liệu Google Maps cho Ăn Uống Quanh Đây

## Cấu trúc
- `common/` — dùng chung 2 máy: `aqd_api.py` (client API admin), `maps_fetch.py` (lấy thông tin quán, không cần trình duyệt), `safe_run.py` (chạy từng quán: list/run, đánh dấu `SAFE_DONE`), `photo_filter.py` (lọc ảnh bằng CLIP), `menu_worker.py` + `menu_ocr.py` + `menu_vocab.json` (OCR ảnh menu → tạo món/danh mục, đọc SĐT bảng hiệu).
- `windows/` — máy Admin: `alt.py` (vòng lặp fetch), `start_fetcher.ps1`, `start_menu_worker.ps1`, `aqd_*.cmd` (đặt trong thư mục Startup để tự chạy khi đăng nhập).
- `mac/` — Mac mini: script cài đặt; launchd plist (`aqd.alt`, `aqd.chrome`, `aqd.menu`) trong `mac/launchd/`.
- `tools/` — `cleanup.py` (sửa/ẩn món sai đã ghi), `build_vocab.py`, các patch.

## Chia việc (từ 14/10/2026)
- Admin lấy **ID chẵn**, Mac lấy **ID lẻ**. `alt.py` liệt kê toàn bộ quán nháp chưa có tên trên server, lọc theo chẵn/lẻ, chạy tăng dần; hết thì ngủ 10 phút rồi liệt kê lại. Quán lỗi 3 lượt bị bỏ qua (`parity_tries.json`).
- Cấu hình: `AQD_ROOT`, `AQD_PARITY` (`even`/`odd`, mặc định Windows=even, Mac=odd).
- Dừng: tạo file `STOP_ALL` trong thư mục gốc (dừng sạch giữa 2 quán). `STOP_MENU` chỉ dừng menu worker.

## Bí mật
Token API nằm trong `creds.env` (KHÔNG commit). Không commit chrome-profile, cookie, dữ liệu `aqd_data/`, log, queue.
