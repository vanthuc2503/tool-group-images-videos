#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tool so sánh ảnh GỐC (M/O) vs ảnh CDN đã tải, theo từng poi_id.
- Xem lần lượt từng POI, bấm Next / Previous (hoặc phím mũi tên trái/phải).
- Bấm "Đánh dấu LỆCH" (hoặc phím M) để ghi/gỡ poi_id khỏi file log .txt.

Cách chạy:
    python compare_tool.py
Rồi mở trình duyệt vào:  http://localhost:8000

Tuỳ chọn:
    python compare_tool.py --csv duong_dan.csv --log mismatch_log.txt --port 8000
"""

import argparse
import html
import re
import os
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pandas as pd

# ----------------------- Cấu hình qua tham số dòng lệnh -----------------------
parser = argparse.ArgumentParser()
parser.add_argument("--csv", default="poi_images_merged.csv", help="Đường dẫn file CSV")
parser.add_argument("--log", default="mismatch_log.txt", help="File log lưu poi_id bị lệch")
parser.add_argument("--port", type=int, default=8000, help="Cổng chạy web")
ARGS = parser.parse_args()

CSV_PATH = ARGS.csv
LOG_PATH = ARGS.log
PORT = ARGS.port

# ----------------------------- Đọc dữ liệu -----------------------------
df = pd.read_csv(CSV_PATH)
df = df.reset_index(drop=True)
N = len(df)


def parse_urls(cell):
    """Tách chuỗi url lộn xộn (ngoặc kép, xuống dòng, dấu phẩy) thành list url."""
    if pd.isna(cell):
        return []
    return [p for p in re.split(r'[\s,"]+', str(cell)) if p.startswith("http")]


# ----------------------- Quản lý danh sách đánh dấu lệch -----------------------
def load_marks():
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        # giữ thứ tự, bỏ dòng trống/trùng
        seen, out = set(), []
        for line in f:
            pid = line.strip()
            if pid and pid not in seen:
                seen.add(pid)
                out.append(pid)
        return out


def save_marks(marks):
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        for pid in marks:
            f.write(pid + "\n")


def toggle_mark(poi_id):
    marks = load_marks()
    if poi_id in marks:
        marks.remove(poi_id)
    else:
        marks.append(poi_id)
    save_marks(marks)
    return poi_id in load_marks()


# ------------------------------ Sinh HTML ------------------------------
def img_tag(u):
    u = html.escape(u, quote=True)
    return (f'<a href="{u}" target="_blank" title="Bấm để mở ảnh gốc">'
            f'<img src="{u}" loading="lazy" '
            f'style="width:150px;height:150px;object-fit:cover;border-radius:8px;'
            f'margin:4px;background:#eee;border:1px solid #ddd"></a>')


def grid(urls):
    if not urls:
        return '<div style="color:#999;font-style:italic;padding:12px">(không có ảnh)</div>'
    return '<div style="display:flex;flex-wrap:wrap">' + "".join(img_tag(u) for u in urls) + '</div>'


def panel(title, count, inner):
    return (f'<div style="flex:1;min-width:0;background:#fafafa;border-radius:8px;padding:10px">'
            f'<div style="font-size:13px;font-weight:600;color:#555;margin-bottom:6px">'
            f'{title} &middot; {count} ảnh</div>{inner}</div>')


def render_page(idx):
    idx = max(0, min(idx, N - 1))
    row = df.iloc[idx]
    pid = str(row["poi_id"])
    name = html.escape(str(row.get("name_M", "")))
    cat = html.escape(str(row.get("category_l2_M", "")))

    co, cc = row.get("cover_image_url_M"), row.get("cover_image_url")
    go = parse_urls(row.get("gallery_urls_O"))
    gc = parse_urls(row.get("gallery_image_url"))
    cover_o = grid([co]) if pd.notna(co) else grid([])
    cover_c = grid([cc]) if pd.notna(cc) else grid([])

    marks = load_marks()
    is_marked = pid in marks
    n_marked = len(marks)

    prev_i = max(0, idx - 1)
    next_i = min(N - 1, idx + 1)

    mark_btn_style = ("background:#d32f2f;color:#fff" if is_marked
                      else "background:#fff;color:#d32f2f;border:2px solid #d32f2f")
    mark_btn_text = "✔ ĐÃ ĐÁNH DẤU LỆCH (bấm để gỡ)" if is_marked else "⚑ Đánh dấu LỆCH (M)"

    pid_e = html.escape(pid)

    return f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>So sánh ảnh — {pid_e}</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;max-width:1100px;margin:0 auto;padding:16px;color:#222">

  <div style="display:flex;justify-content:space-between;align-items:center;
              position:sticky;top:0;background:#fff;padding:10px 0;border-bottom:1px solid #eee;z-index:10">
    <div>
      <a href="/poi?i={prev_i}" style="text-decoration:none;padding:8px 16px;background:#eee;border-radius:6px;color:#222;font-weight:600">&larr; Previous</a>
      <a href="/poi?i={next_i}" style="text-decoration:none;padding:8px 16px;background:#eee;border-radius:6px;color:#222;font-weight:600;margin-left:6px">Next &rarr;</a>
    </div>
    <div style="font-size:14px;color:#666">
      POI <b>{idx + 1}</b> / {N} &nbsp;|&nbsp; Đã đánh dấu: <b style="color:#d32f2f">{n_marked}</b>
    </div>
    <a href="/toggle?poi={pid_e}&i={idx}"
       style="text-decoration:none;padding:8px 16px;border-radius:6px;font-weight:700;{mark_btn_style}">
       {mark_btn_text}</a>
  </div>

  <h2 style="margin:16px 0 2px">{pid_e} — {name}</h2>
  <div style="color:#888;font-size:13px;margin-bottom:16px">{cat}</div>

  <div style="font-size:15px;font-weight:700;margin:10px 0 4px">COVER</div>
  <div style="display:flex;gap:16px;margin-bottom:20px">
    {panel("Gốc (M)", 1 if pd.notna(co) else 0, cover_o)}
    {panel("CDN đã tải", 1 if pd.notna(cc) else 0, cover_c)}
  </div>

  <div style="font-size:15px;font-weight:700;margin:10px 0 4px">GALLERY</div>
  <div style="display:flex;gap:16px">
    {panel("Gốc (O)", len(go), grid(go))}
    {panel("CDN đã tải", len(gc), grid(gc))}
  </div>

  <div style="margin-top:24px;color:#aaa;font-size:12px">
    Phím tắt: &larr; / &rarr; chuyển POI &nbsp;&middot;&nbsp; phím <b>M</b> đánh dấu lệch.
    Log lưu tại: <code>{html.escape(os.path.abspath(LOG_PATH))}</code>
  </div>

<script>
  const prev = "/poi?i={prev_i}", next = "/poi?i={next_i}", toggle = "/toggle?poi={pid_e}&i={idx}";
  document.addEventListener("keydown", (e) => {{
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (e.key === "ArrowLeft")  window.location.href = prev;
    if (e.key === "ArrowRight") window.location.href = next;
    if (e.key === "m" || e.key === "M") window.location.href = toggle;
  }});
</script>
</body></html>"""


# ------------------------------ Web server ------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path in ("/", "/poi"):
            try:
                idx = int(qs.get("i", ["0"])[0])
            except ValueError:
                idx = 0
            self._send(render_page(idx))

        elif parsed.path == "/toggle":
            poi = qs.get("poi", [""])[0]
            try:
                idx = int(qs.get("i", ["0"])[0])
            except ValueError:
                idx = 0
            if poi:
                toggle_mark(poi)
            self._redirect(f"/poi?i={idx}")

        else:
            self._send("404", status=404, ctype="text/plain; charset=utf-8")

    def log_message(self, *args):
        pass  # tắt log ồn ào ra terminal


def main():
    print(f"Đọc {N} POI từ: {os.path.abspath(CSV_PATH)}")
    print(f"Log lệch lưu tại: {os.path.abspath(LOG_PATH)}")
    print(f"\n  >>> Mở trình duyệt vào:  http://localhost:{PORT}\n")
    print("Nhấn Ctrl+C để dừng.")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        server.server_close()


if __name__ == "__main__":
    main()