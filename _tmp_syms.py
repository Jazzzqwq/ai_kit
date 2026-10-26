# -*- coding: utf-8 -*-
import ast, io, sys

out = io.open("_tmp_syms.txt", "w", encoding="utf-8")
for path in sys.argv[1:]:
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    out.write(f"\n===== {path}  ({len(src.splitlines())} lines) =====\n")
    for node in tree.body:
        out.write(f"{node.lineno:5d} {type(node).__name__:12s} {getattr(node, 'name', '')}\n")
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.write(f"      {sub.lineno:5d} .{sub.name}\n")
            out.write("")
out.close()
print("ok")
