"""Wrap site/app.html into a standalone page: python build_site.py [outdir] [dashboard.json]"""
import os
import shutil
import sys

out = sys.argv[1] if len(sys.argv) > 1 else "_site"
data = sys.argv[2] if len(sys.argv) > 2 else "docs/dashboard.json"
frag = open(os.path.join("site", "app.html"), encoding="utf-8").read()
head = (
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    '<meta name="theme-color" content="#0C1218">\n</head>\n<body>\n'
)
os.makedirs(out, exist_ok=True)
open(os.path.join(out, "index.html"), "w", encoding="utf-8").write(head + frag + "\n</body>\n</html>\n")
if os.path.exists(data):
    shutil.copy(data, os.path.join(out, "dashboard.json"))
    print("site built with live data:", data)
else:
    shutil.copy(os.path.join("site", "demo", "dashboard.json"), os.path.join(out, "dashboard.json"))
    print("site built with sample data (no", data + " yet)")
