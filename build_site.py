"""Build the website into a folder. Usage: python build_site.py [outdir] [dashboard.json]"""
import json
import os
import shutil
import sys
from datetime import datetime, timezone

out = sys.argv[1] if len(sys.argv) > 1 else "_site"
data = sys.argv[2] if len(sys.argv) > 2 else "docs/dashboard.json"


def first(*paths):
    return next((p for p in paths if os.path.exists(p)), None)


app = first("app.html", "site/app.html")
if not app:
    sys.exit("app.html was not found in the repo")
frag = open(app, encoding="utf-8").read()
head = (
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    '<meta name="theme-color" content="#0C1218">\n</head>\n<body>\n'
)
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
    f.write(head + frag + "\n</body>\n</html>\n")

target = os.path.join(out, "dashboard.json")
if os.path.exists(data):
    shutil.copy(data, target)
    print("site built with live data:", data)
else:
    demo = first("demo-dashboard.json", "site/demo/dashboard.json", "dashboard.json")
    if demo:
        shutil.copy(demo, target)
        print("site built with sample data:", demo)
    else:
        with open(target, "w") as f:
            json.dump({"generated": datetime.now(timezone.utc).isoformat(), "rows": [], "health": {}, "queue": []}, f)
        print("site built with an empty results file")
