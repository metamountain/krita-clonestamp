"""CLI to the Krita bridge (same code as the MCP server).
    python kcall.py exec "<python>"      python kcall.py exec -f script.py
    python kcall.py shot out.png         python kcall.py canvas out.png
    python kcall.py errors | plugins [name]
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import krita_mcp as k
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "exec":
    code = open(args[1], encoding="utf-8").read() if args[0] == "-f" else args[0]
    print(k.execute_python(code))
elif cmd in ("shot", "canvas"):
    img = k.screenshot() if cmd == "shot" else k.canvas_image()
    open(args[0], "wb").write(img.data); print("saved", args[0])
elif cmd == "errors":
    print(k.get_errors())
elif cmd == "plugins":
    print(k.plugin_status(args[0] if args else ""))
