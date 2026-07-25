#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TOOL: Group ảnh S3 + So sánh với file Food gốc + Đánh dấu trùng/lệch + Export.

Luồng hoạt động:
  1. Chạy app, upload 2 file raw_s3 + raw_v1_3 từ local.
  2. Xem preview 10 dòng đầu của mỗi file, bấm Import để nạp dữ liệu.
  3. raw_s3 được gộp cover + gallery theo poi_id.
  4. Mở web xem từng POI: ảnh S3 vs ảnh gốc, bấm Trùng / Lệch -> lưu log .txt.
  5. Bấm Export -> tạo Food_v1_3_new.csv:
       - POI trùng (có ảnh S3, không bị đánh Lệch): fill link S3 vào
         cột cover_image_url & gallery_urls (nhiều link cách nhau bởi dấu phẩy).
       - POI không trùng: fill "No s3 image".
  6. Bấm Export grouped -> tạo file raw_s3 đã gộp, chỉ gồm:
       POI_id, cover_image_url, gallery_image_url.

Cách chạy:
    python group_compare_tool.py
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
import shutil
import uuid
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pandas as pd

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import cgi

NO_IMG = "No s3 image"
UPLOAD_DIR = "uploads"
EXPORT_DIR = "exports"

# ------------------------------ Tham số ------------------------------
ap = argparse.ArgumentParser()
ap.add_argument("raw_s3", nargs="?", help="File ảnh S3")
ap.add_argument("raw_v1_3", nargs="?", help="File Food v1.3 gốc")
ap.add_argument("--raw", dest="raw_s3_opt", help="Alias cũ cho raw_s3")
ap.add_argument("--food", dest="raw_v1_3_opt", help="Alias cũ cho raw_v1_3")
ap.add_argument("--log", default="compare_log.txt", help="File log trùng/lệch")
ap.add_argument("--out", default=None, help="File xuất (mặc định <food>_new.csv)")
ap.add_argument("--grouped-out", default=None, help="File xuất raw_s3 đã gộp (mặc định <raw_s3>_grouped.csv)")
ap.add_argument("--port", type=int, default=8000)
ap.add_argument("--export-mode", choices=["auto", "manual"], default="auto")
ARGS = ap.parse_args()

RAW_PATH = ARGS.raw_s3 or ARGS.raw_s3_opt
FOOD_PATH = ARGS.raw_v1_3 or ARGS.raw_v1_3_opt
if (RAW_PATH and not FOOD_PATH) or (FOOD_PATH and not RAW_PATH):
    ap.error("nếu truyền file bằng command line thì cần đủ 2 file: raw_s3 và raw_v1_3")
LOG_PATH = ARGS.log
PORT = ARGS.port
EXPORT_MODE = ARGS.export_mode
OUT_PATH = ARGS.out
GROUPED_OUT_PATH = ARGS.grouped_out

S3_COVER = {}
S3_GALLERY_STR = {}
S3_GALLERY_LIST = {}
S3_POIS = []
FOOD = None
FOOD_POIS = set()
REVIEW = []
FOOD_SRC = None


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


def safe_filename(name):
    name = os.path.basename(name or "")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or "uploaded.csv"


def default_output_path(input_path, suffix, output_dir=None):
    base_name = os.path.basename(input_path)
    base, ext = os.path.splitext(base_name)
    filename = f"{base}_{suffix}{ext or '.csv'}"
    return os.path.join(output_dir, filename) if output_dir else os.path.join(os.path.dirname(input_path), filename)


def preview_csv(path, header=0):
    try:
        return pd.read_csv(path, nrows=10, dtype=str, header=header).fillna("")
    except Exception as e:
        return pd.DataFrame([{"Lỗi đọc file": str(e)}])


def load_inputs(raw_path, food_path, output_dir=None):
    global RAW_PATH, FOOD_PATH, OUT_PATH, GROUPED_OUT_PATH
    global S3_COVER, S3_GALLERY_STR, S3_GALLERY_LIST, S3_POIS
    global FOOD, FOOD_POIS, REVIEW, FOOD_SRC

    RAW_PATH = raw_path
    FOOD_PATH = food_path
    OUT_PATH = ARGS.out or default_output_path(FOOD_PATH, "new", output_dir)
    GROUPED_OUT_PATH = ARGS.grouped_out or default_output_path(RAW_PATH, "grouped", output_dir)

    S3_COVER, S3_GALLERY_STR, S3_GALLERY_LIST, S3_POIS = group_s3(RAW_PATH)
    FOOD = load_food(FOOD_PATH)
    FOOD_POIS = set(FOOD["poi_id"])
    REVIEW = [p for p in S3_POIS if p in FOOD_POIS]
    FOOD_SRC = FOOD.set_index("poi_id")


def data_loaded():
    return FOOD is not None


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
    out_dir = os.path.dirname(OUT_PATH)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    out.to_csv(OUT_PATH, index=False)
    return len(out), n_fill, n_no


def do_export_grouped():
    rows = []
    for pid in S3_POIS:
        rows.append({
            "POI_id": pid,
            "cover_image_url": S3_COVER.get(pid, NO_IMG),
            "gallery_image_url": S3_GALLERY_STR.get(pid, NO_IMG),
        })
    out = pd.DataFrame(rows, columns=["POI_id", "cover_image_url", "gallery_image_url"])
    out_dir = os.path.dirname(GROUPED_OUT_PATH)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    out.to_csv(GROUPED_OUT_PATH, index=False)
    return len(out)


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


def render_table(df):
    return df.to_html(index=False, escape=True, border=0, classes="preview-table")


def page_shell(title, body):
    return f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family:system-ui,-apple-system,sans-serif;max-width:1180px;margin:0 auto;padding:18px;color:#222; }}
  input[type=file] {{ display:block;margin-top:8px; }}
  label {{ display:block;font-size:14px;font-weight:700;color:#333; }}
  .row {{ display:grid;grid-template-columns:1fr 1fr;gap:16px; }}
  .box {{ background:#fafafa;border:1px solid #e4e4e4;border-radius:8px;padding:14px; }}
  .preview-wrap {{ overflow:auto;border:1px solid #e5e5e5;border-radius:8px;max-height:360px;background:#fff; }}
  .preview-table {{ border-collapse:collapse;font-size:12px;min-width:100%; }}
  .preview-table th,.preview-table td {{ border-bottom:1px solid #eee;padding:6px 8px;text-align:left;white-space:nowrap; }}
  .preview-table th {{ background:#f5f7f9;position:sticky;top:0;z-index:1; }}
  .btn {{ display:inline-block;text-decoration:none;border:0;border-radius:8px;padding:11px 18px;font-weight:700;font-size:15px;cursor:pointer; }}
  .primary {{ background:#1565c0;color:#fff; }}
  .secondary {{ background:#eee;color:#222; }}
  .muted {{ color:#777;font-size:13px; }}
  @media (max-width: 800px) {{ .row {{ grid-template-columns:1fr; }} }}
</style></head><body>{body}</body></html>"""


def render_upload(error=None):
    error_html = f'<p style="color:#c62828;font-weight:700">{html.escape(error)}</p>' if error else ""
    body = f"""
<h2 style="margin:0 0 8px">Import 2 file CSV</h2>
<p class="muted" style="margin-top:0">Chọn file raw_s3 và raw_v1_3 từ máy local, sau đó xem preview 10 dòng đầu trước khi import.</p>
{error_html}
<form method="post" action="/preview" enctype="multipart/form-data">
  <div class="row">
    <div class="box">
      <label>File 1: raw_s3</label>
      <input type="file" name="raw_s3" accept=".csv,text/csv" required>
    </div>
    <div class="box">
      <label>File 2: raw_v1_3</label>
      <input type="file" name="raw_v1_3" accept=".csv,text/csv" required>
    </div>
  </div>
  <div style="margin-top:16px">
    <button class="btn primary" type="submit">Preview 10 dòng đầu</button>
  </div>
</form>"""
    return page_shell("Import CSV", body)


def render_preview(raw_path, food_path, error=None):
    raw_preview = preview_csv(raw_path)
    food_preview = preview_csv(food_path)
    error_html = f'<p style="color:#c62828;font-weight:700">{html.escape(error)}</p>' if error else ""
    body = f"""
<h2 style="margin:0 0 8px">Preview file đã chọn</h2>
<p class="muted" style="margin-top:0">Nếu đúng file, bấm Import để bắt đầu review ảnh.</p>
{error_html}
<div class="row">
  <div>
    <h3 style="margin:10px 0 6px">raw_s3</h3>
    <p class="muted"><code>{html.escape(os.path.basename(raw_path))}</code></p>
    <div class="preview-wrap">{render_table(raw_preview)}</div>
  </div>
  <div>
    <h3 style="margin:10px 0 6px">raw_v1_3</h3>
    <p class="muted"><code>{html.escape(os.path.basename(food_path))}</code></p>
    <div class="preview-wrap">{render_table(food_preview)}</div>
  </div>
</div>
<form method="post" action="/import" style="margin-top:16px;display:flex;gap:8px;flex-wrap:wrap">
  <input type="hidden" name="raw_path" value="{html.escape(raw_path, quote=True)}">
  <input type="hidden" name="food_path" value="{html.escape(food_path, quote=True)}">
  <button class="btn primary" type="submit">Import và bắt đầu review</button>
  <a class="btn secondary" href="/">Chọn lại file</a>
</form>"""
    return page_shell("Preview CSV", body)


def render_page(idx):
    if not data_loaded():
        return render_upload()
    if not REVIEW:
        return page_shell("Không có dữ liệu review", '<h2>Không có POI nào có ảnh S3 để review.</h2><p><a class="btn secondary" href="/">Import file khác</a></p>')
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
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <a href="/export-grouped" style="text-decoration:none;padding:12px 22px;background:#455a64;color:#fff;border-radius:8px;font-weight:700;font-size:15px">
        ⬇ EXPORT GROUPED</a>
      <a href="/export" style="text-decoration:none;padding:12px 22px;background:#1565c0;color:#fff;border-radius:8px;font-weight:700;font-size:15px">
        ⬇ EXPORT {html.escape(os.path.basename(OUT_PATH))}</a>
    </div>
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


def render_grouped_export(total):
    return f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8"><title>Đã export grouped</title></head>
<body style="font-family:system-ui,sans-serif;max-width:700px;margin:40px auto;padding:16px">
  <h2>✅ Đã export grouped xong</h2>
  <p>File: <code>{html.escape(os.path.abspath(GROUPED_OUT_PATH))}</code></p>
  <ul style="line-height:1.8">
    <li>Tổng số POI đã group: <b>{total}</b></li>
    <li>Cột xuất ra: <code>POI_id</code>, <code>cover_image_url</code>, <code>gallery_image_url</code></li>
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

    def _save_upload(self, form, field_name):
        if field_name not in form:
            raise ValueError(f"Thiếu file {field_name}")
        item = form[field_name]
        if isinstance(item, list):
            item = item[0]
        if not item.filename:
            raise ValueError(f"Chưa chọn file {field_name}")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        filename = f"{uuid.uuid4().hex}_{safe_filename(item.filename)}"
        path = os.path.join(UPLOAD_DIR, filename)
        with open(path, "wb") as f:
            shutil.copyfileobj(item.file, f)
        return path

    def _parse_multipart(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("Form upload không đúng định dạng multipart/form-data")
        return cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            },
        )

    def _parse_urlencoded(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        return parse_qs(body)

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
            if not data_loaded():
                self._redirect("/")
                return
            total, n_fill, n_no = do_export()
            self._send(render_export(total, n_fill, n_no))
        elif u.path == "/export-grouped":
            if not data_loaded():
                self._redirect("/")
                return
            total = do_export_grouped()
            self._send(render_grouped_export(total))
        else:
            self._send("404", status=404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/preview":
            try:
                form = self._parse_multipart()
                raw_path = self._save_upload(form, "raw_s3")
                food_path = self._save_upload(form, "raw_v1_3")
                self._send(render_preview(raw_path, food_path))
            except Exception as e:
                self._send(render_upload(str(e)), status=400)
        elif u.path == "/import":
            raw_path = ""
            food_path = ""
            try:
                form = self._parse_urlencoded()
                raw_path = form.get("raw_path", [""])[0]
                food_path = form.get("food_path", [""])[0]
                if not raw_path or not food_path:
                    raise ValueError("Thiếu đường dẫn file import")
                if not os.path.exists(raw_path) or not os.path.exists(food_path):
                    raise ValueError("File preview không còn tồn tại, vui lòng chọn lại file")
                load_inputs(raw_path, food_path, output_dir=EXPORT_DIR)
                self._redirect("/poi?i=0")
            except Exception as e:
                if raw_path and food_path:
                    self._send(render_preview(raw_path, food_path, str(e)), status=400)
                else:
                    self._send(render_upload(str(e)), status=400)
        else:
            self._send("404", status=404)

    def log_message(self, *a):
        pass


def main():
    if RAW_PATH and FOOD_PATH:
        load_inputs(RAW_PATH, FOOD_PATH)
        print(f"Ảnh S3     : {os.path.abspath(RAW_PATH)}  ({len(S3_POIS)} POI có ảnh)")
        print(f"Food gốc   : {os.path.abspath(FOOD_PATH)}  ({len(FOOD)} dòng)")
        print(f"Review     : {len(REVIEW)} POI (có ảnh S3 & có trong Food)")
    else:
        print("Chưa import dữ liệu. Mở web để chọn 2 file raw_s3 và raw_v1_3 từ local.")
    print(f"Log        : {os.path.abspath(LOG_PATH)}")
    if data_loaded():
        print(f"Sẽ export  : {os.path.abspath(OUT_PATH)}  (mode={EXPORT_MODE})")
        print(f"Grouped    : {os.path.abspath(GROUPED_OUT_PATH)}")
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
