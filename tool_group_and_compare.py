#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TOOL: Group ảnh S3 + So sánh với file Food gốc + Đánh dấu trùng/lệch + Export.

Luồng hoạt động:
  1. Đọc file raw_s3 (ảnh đã up S3) -> gộp cover + gallery theo poi_id.
  2. Đọc file raw_v1_3 (file gốc) -> lấy ảnh nguồn để đối chiếu.
  3. Mở web xem từng POI: ảnh S3 vs ảnh gốc, bấm Trùng / Lệch -> lưu log .txt.
  4. Bấm Export -> tạo Food_v1_3_new.csv:
       - POI trùng (có ảnh S3, không bị đánh Lệch): fill link S3 vào
         cột cover_image_url & gallery_urls (nhiều link cách nhau bởi dấu phẩy).
       - POI không trùng: fill "No s3 image".

Cách chạy:
    python group_compare_tool.py raw_s3.csv raw_v1_3.csv
Rồi mở:  http://localhost:8000

Tuỳ chọn:
    python group_compare_tool.py raw_s3.csv raw_v1_3.csv \
        --log compare_log.txt --port 8000 --export-mode auto

Alias cũ vẫn dùng được:
    python group_compare_tool.py --raw raw_s3.csv --food raw_v1_3.csv

  --export-mode auto   : fill mọi POI có ảnh S3, TRỪ cái bị đánh "Lệch" (mặc định)
  --export-mode manual : chỉ fill POI được đánh "Trùng"
"""

import argparse
import html
import os
import re
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pandas as pd

NO_IMG = "No s3 image"

# ------------------------------ Tham số ------------------------------
ap = argparse.ArgumentParser()
ap.add_argument("raw_s3", nargs="?", help="File ảnh S3")
ap.add_argument("raw_v1_3", nargs="?", help="File Food v1.3 gốc")
ap.add_argument("--raw", dest="raw_s3_opt", help="Alias cũ cho raw_s3")
ap.add_argument("--food", dest="raw_v1_3_opt", help="Alias cũ cho raw_v1_3")
ap.add_argument("--log", default="compare_log.txt", help="File log trùng/lệch")
ap.add_argument("--out", default=None, help="File xuất (mặc định <food>_new.csv)")
ap.add_argument("--port", type=int, default=8000)
ap.add_argument("--export-mode", choices=["auto", "manual"], default="auto")
ARGS = ap.parse_args()

RAW_PATH = ARGS.raw_s3 or ARGS.raw_s3_opt
FOOD_PATH = ARGS.raw_v1_3 or ARGS.raw_v1_3_opt
if not RAW_PATH or not FOOD_PATH:
    ap.error("cần truyền 2 file: raw_s3 và raw_v1_3. Ví dụ: tool_group_and_compare.py raw_s3.csv raw_v1_3.csv")
LOG_PATH = ARGS.log
PORT = ARGS.port
EXPORT_MODE = ARGS.export_mode
if ARGS.out:
    OUT_PATH = ARGS.out
else:
    base, ext = os.path.splitext(FOOD_PATH)
    OUT_PATH = f"{base}_new{ext or '.csv'}"


# --------------------------- Xử lý dữ liệu ---------------------------
def parse_urls(cell):
    """Tách chuỗi url lộn xộn (ngoặc kép, xuống dòng, dấu phẩy) thành list url."""
    if pd.isna(cell):
        return []
    return [p for p in re.split(r'[\s,"]+', str(cell)) if p.startswith("http")]


def group_s3(raw_path):
    """Gộp ảnh S3 theo poi_id -> (dict cover, dict gallery-chuoi, dict gallery-list)."""
    raw = pd.read_csv(raw_path)
    cover = (raw[raw["image_type"] == "cover_image_url"]
             .groupby("poi_id")["cdn_url"].first())
    gallery = (raw[raw["image_type"] == "gallery_urls"]
               .groupby("poi_id")["cdn_url"].apply(list))
    s3_cover = cover.to_dict()
    s3_gallery_list = {k: v for k, v in gallery.to_dict().items()}
    s3_gallery_str = {k: ",".join(v) for k, v in s3_gallery_list.items()}
    all_pois = sorted(set(s3_cover) | set(s3_gallery_list))
    return s3_cover, s3_gallery_str, s3_gallery_list, all_pois


def load_food(food_path):
    """Đọc Food gốc -> DataFrame sạch (tên cột thật, bỏ cột/hàng rác)."""
    food = pd.read_csv(food_path, header=1)
    food = food.loc[:, ~food.columns.astype(str).str.startswith("Unnamed")]
    food = food.drop(columns=["337"], errors="ignore")
    food = food.drop(index=0).reset_index(drop=True)          # bỏ dòng [M]/[O]
    food = food[food["poi_id"].notna()].reset_index(drop=True)  # bỏ dòng rỗng
    return food


# Nạp dữ liệu 1 lần lúc khởi động
S3_COVER, S3_GALLERY_STR, S3_GALLERY_LIST, S3_POIS = group_s3(RAW_PATH)
FOOD = load_food(FOOD_PATH)
FOOD_POIS = set(FOOD["poi_id"])

# Danh sách để review = các POI có ảnh S3 và có trong Food ("id đã gộp")
REVIEW = [p for p in S3_POIS if p in FOOD_POIS]

# Ảnh nguồn (gốc) của Food để đối chiếu, tra theo poi_id
FOOD_SRC = FOOD.set_index("poi_id")


# ----------------------- Log trùng / lệch -----------------------
def load_marks():
    """Đọc log -> dict {poi_id: 'trung'|'lech'}."""
    marks = {}
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) >= 2:
                    marks[parts[0].strip()] = parts[1].strip()
    return marks


def save_marks(marks):
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        for pid, st in marks.items():
            f.write(f"{pid},{st}\n")


def set_mark(poi_id, status):
    marks = load_marks()
    if status == "clear":
        marks.pop(poi_id, None)
    else:
        marks[poi_id] = status
    save_marks(marks)


# ------------------------------ Export ------------------------------
def should_fill(poi_id, marks):
    """Quyết định POI này có được fill ảnh S3 hay không."""
    st = marks.get(poi_id)
    has_s3 = poi_id in set(S3_COVER) or poi_id in set(S3_GALLERY_STR)
    if EXPORT_MODE == "manual":
        return st == "trung"
    # auto: có ảnh S3 và không bị đánh lệch
    return has_s3 and st != "lech"


def do_export():
    marks = load_marks()
    out = FOOD.copy()
    n_fill, n_no = 0, 0

    covers, galleries = [], []
    for pid in out["poi_id"]:
        if should_fill(pid, marks):
            covers.append(S3_COVER.get(pid, NO_IMG))
            galleries.append(S3_GALLERY_STR.get(pid, NO_IMG))
            n_fill += 1
        else:
            covers.append(NO_IMG)
            galleries.append(NO_IMG)
            n_no += 1

    out["cover_image_url"] = covers
    out["gallery_urls"] = galleries
    out.to_csv(OUT_PATH, index=False)
    return len(out), n_fill, n_no


# ------------------------------ HTML ------------------------------
def img_tag(u):
    u = html.escape(u, quote=True)
    return (f'<a href="{u}" target="_blank" title="Mở ảnh gốc">'
            f'<img src="{u}" loading="lazy" '
            f'style="width:150px;height:150px;object-fit:cover;border-radius:8px;'
            f'margin:4px;background:#eee;border:1px solid #ddd"></a>')


def grid(urls):
    if not urls:
        return '<div style="color:#999;font-style:italic;padding:12px">(không có ảnh)</div>'
    return '<div style="display:flex;flex-wrap:wrap">' + "".join(img_tag(u) for u in urls) + '</div>'


def panel(title, count, inner, bg="#fafafa"):
    return (f'<div style="flex:1;min-width:0;background:{bg};border-radius:8px;padding:10px">'
            f'<div style="font-size:13px;font-weight:600;color:#555;margin-bottom:6px">'
            f'{title} &middot; {count} ảnh</div>{inner}</div>')


def render_page(idx):
    if not REVIEW:
        return "<h2>Không có POI nào có ảnh S3 để review.</h2>"
    idx = max(0, min(idx, len(REVIEW) - 1))
    pid = REVIEW[idx]
    marks = load_marks()
    st = marks.get(pid)

    # thông tin từ Food
    row = FOOD_SRC.loc[pid] if pid in FOOD_SRC.index else None
    name = html.escape(str(row["name"])) if row is not None else ""
    cat = html.escape(str(row.get("category_l2", ""))) if row is not None else ""

    # Ảnh S3
    s3_cover = S3_COVER.get(pid)
    s3_gal = S3_GALLERY_LIST.get(pid, [])
    # Ảnh gốc từ Food
    src_cover = row["cover_image_url"] if row is not None else None
    src_gal = parse_urls(row["gallery_urls"]) if row is not None else []

    s3_cover_grid = grid([s3_cover]) if s3_cover else grid([])
    src_cover_grid = grid([src_cover]) if (src_cover is not None and pd.notna(src_cover)) else grid([])

    n_trung = sum(1 for v in marks.values() if v == "trung")
    n_lech = sum(1 for v in marks.values() if v == "lech")

    prev_i, next_i = max(0, idx - 1), min(len(REVIEW) - 1, idx + 1)
    pid_e = html.escape(pid)

    def btn(label, status, active_color):
        active = (st == status)
        style = (f"background:{active_color};color:#fff" if active
                 else f"background:#fff;color:{active_color};border:2px solid {active_color}")
        return (f'<a href="/toggle?poi={pid_e}&st={status}&i={idx}" '
                f'style="text-decoration:none;padding:8px 16px;border-radius:6px;'
                f'font-weight:700;margin-left:6px;{style}">{label}</a>')

    status_badge = ""
    if st == "trung":
        status_badge = '<span style="color:#2e7d32;font-weight:700">● Đã đánh TRÙNG</span>'
    elif st == "lech":
        status_badge = '<span style="color:#d32f2f;font-weight:700">● Đã đánh LỆCH</span>'
    else:
        status_badge = '<span style="color:#999">● Chưa đánh dấu</span>'

    return f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>So sánh — {pid_e}</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;max-width:1150px;margin:0 auto;padding:16px;color:#222">

  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;
              position:sticky;top:0;background:#fff;padding:10px 0;border-bottom:1px solid #eee;z-index:10">
    <div>
      <a href="/poi?i={prev_i}" style="text-decoration:none;padding:8px 16px;background:#eee;border-radius:6px;color:#222;font-weight:600">&larr; Previous</a>
      <a href="/poi?i={next_i}" style="text-decoration:none;padding:8px 16px;background:#eee;border-radius:6px;color:#222;font-weight:600;margin-left:6px">Next &rarr;</a>
    </div>
    <div style="font-size:14px;color:#666">
      POI <b>{idx + 1}</b> / {len(REVIEW)} &nbsp;|&nbsp;
      Trùng: <b style="color:#2e7d32">{n_trung}</b> &nbsp;
      Lệch: <b style="color:#d32f2f">{n_lech}</b>
    </div>
    <div>
      {btn("✔ Trùng (T)", "trung", "#2e7d32")}
      {btn("⚑ Lệch (L)", "lech", "#d32f2f")}
      <a href="/toggle?poi={pid_e}&st=clear&i={idx}" style="text-decoration:none;padding:8px 12px;border-radius:6px;color:#666;margin-left:6px;border:1px solid #ccc">Bỏ</a>
    </div>
  </div>

  <div style="display:flex;justify-content:space-between;align-items:baseline;margin:16px 0 2px">
    <h2 style="margin:0">{pid_e} — {name}</h2>
    <div>{status_badge}</div>
  </div>
  <div style="color:#888;font-size:13px;margin-bottom:16px">{cat}</div>

  <div style="font-size:15px;font-weight:700;margin:10px 0 4px">COVER</div>
  <div style="display:flex;gap:16px;margin-bottom:20px">
    {panel("Ảnh S3", 1 if s3_cover else 0, s3_cover_grid, bg="#eef6ff")}
    {panel("Ảnh gốc (Food)", 1 if (src_cover is not None and pd.notna(src_cover)) else 0, src_cover_grid)}
  </div>

  <div style="font-size:15px;font-weight:700;margin:10px 0 4px">GALLERY</div>
  <div style="display:flex;gap:16px">
    {panel("Ảnh S3", len(s3_gal), grid(s3_gal), bg="#eef6ff")}
    {panel("Ảnh gốc (Food)", len(src_gal), grid(src_gal))}
  </div>

  <div style="margin-top:24px;padding-top:16px;border-top:1px solid #eee;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
    <div style="color:#aaa;font-size:12px">
      Phím tắt: &larr;/&rarr; chuyển &middot; <b>T</b> Trùng &middot; <b>L</b> Lệch.<br>
      Log: <code>{html.escape(os.path.abspath(LOG_PATH))}</code>
    </div>
    <a href="/export" style="text-decoration:none;padding:12px 22px;background:#1565c0;color:#fff;border-radius:8px;font-weight:700;font-size:15px">
      ⬇ EXPORT {html.escape(os.path.basename(OUT_PATH))}</a>
  </div>

<script>
  const prev="/poi?i={prev_i}", next="/poi?i={next_i}";
  const t="/toggle?poi={pid_e}&st=trung&i={idx}", l="/toggle?poi={pid_e}&st=lech&i={idx}";
  document.addEventListener("keydown",(e)=>{{
    if(e.target.tagName==="INPUT"||e.target.tagName==="TEXTAREA")return;
    if(e.key==="ArrowLeft")location.href=prev;
    if(e.key==="ArrowRight")location.href=next;
    if(e.key==="t"||e.key==="T")location.href=t;
    if(e.key==="l"||e.key==="L")location.href=l;
  }});
</script>
</body></html>"""


def render_export(total, n_fill, n_no):
    return f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8"><title>Đã export</title></head>
<body style="font-family:system-ui,sans-serif;max-width:700px;margin:40px auto;padding:16px">
  <h2>✅ Đã export xong</h2>
  <p>File: <code>{html.escape(os.path.abspath(OUT_PATH))}</code></p>
  <ul style="line-height:1.8">
    <li>Tổng số dòng: <b>{total}</b></li>
    <li>Số POI được fill ảnh S3 (trùng): <b style="color:#2e7d32">{n_fill}</b></li>
    <li>Số POI ghi "{NO_IMG}": <b style="color:#d32f2f">{n_no}</b></li>
    <li>Chế độ export: <b>{EXPORT_MODE}</b></li>
  </ul>
  <a href="/poi?i=0" style="text-decoration:none;padding:10px 18px;background:#eee;border-radius:6px;color:#222;font-weight:600">&larr; Quay lại review</a>
</body></html>"""


# ------------------------------ Server ------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, body, status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/poi"):
            try:
                idx = int(qs.get("i", ["0"])[0])
            except ValueError:
                idx = 0
            self._send(render_page(idx))
        elif u.path == "/toggle":
            poi = qs.get("poi", [""])[0]
            stt = qs.get("st", ["clear"])[0]
            try:
                idx = int(qs.get("i", ["0"])[0])
            except ValueError:
                idx = 0
            if poi:
                set_mark(poi, stt)
            self._redirect(f"/poi?i={idx}")
        elif u.path == "/export":
            total, n_fill, n_no = do_export()
            self._send(render_export(total, n_fill, n_no))
        else:
            self._send("404", status=404)

    def log_message(self, *a):
        pass


def main():
    print(f"Ảnh S3     : {os.path.abspath(RAW_PATH)}  ({len(S3_POIS)} POI có ảnh)")
    print(f"Food gốc   : {os.path.abspath(FOOD_PATH)}  ({len(FOOD)} dòng)")
    print(f"Review     : {len(REVIEW)} POI (có ảnh S3 & có trong Food)")
    print(f"Log        : {os.path.abspath(LOG_PATH)}")
    print(f"Sẽ export  : {os.path.abspath(OUT_PATH)}  (mode={EXPORT_MODE})")
    print(f"\n  >>> Mở trình duyệt:  http://localhost:{PORT}\n")
    print("Ctrl+C để dừng.")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        srv.server_close()


if __name__ == "__main__":
    main()
