"""Security smoke test for repository tools (manual verification script)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from harness_agent.tools.repository_tools import (  # noqa: E402
    analyze_dependencies,
    list_directory,
    read_file,
    search_code,
)

cases = [
    ("read ../../something", lambda: read_file("../../something")),
    ("read .env", lambda: read_file(".env")),
    ("list ../", lambda: list_directory("../")),
    ("read abs ssh key", lambda: read_file(r"C:\Users\xxx\.ssh\id_rsa")),
    ("read /etc/passwd", lambda: read_file("/etc/passwd")),
    ("search empty query", lambda: search_code("", path=".")),
    ("search .venv", lambda: search_code("api", path=".venv")),
    ("search .git", lambda: search_code("api", path=".git")),
]

for name, fn in cases:
    r = fn()
    print(f"{name}: ok={r['ok']} err={r.get('error')}")

allowed = read_file(".env.example")
print("read .env.example: ok=", allowed["ok"], "lines=", allowed.get("total_lines"))

binary = read_file(".venv/Scripts/python.exe")
print("read python.exe: ok=", binary["ok"], "err=", binary.get("error"))

r = search_code("api_key", file_pattern="*.py")
hits = [(m["path"], m["line"]) for m in r["matches"]]
print("search api_key *.py:", r["ok"], "matches=", r["match_count"], hits[:6])

d = analyze_dependencies()
print(
    "deps:",
    d["ok"],
    [(m["type"], m.get("name") or m.get("path")) for m in d["manifests"]],
)
