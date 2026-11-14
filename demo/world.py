#!/usr/bin/env python3
"""Build a person's world for the demo: a real project, a data file, notes, photos, a shell start-up file.

Usage: python3 demo/world.py DIR
Everything here is generated. Nothing comes from a real person.
"""
import os
import shutil
import sys

root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "./person")
proj = os.path.join(root, "project")
shutil.rmtree(root, ignore_errors=True)
for d in ("data", "docs", "photos"):
    os.makedirs(os.path.join(proj, d))
lib = os.path.dirname(os.__file__)
for pkg in ("json", "email", "http", "urllib", "logging"):
    shutil.copytree(os.path.join(lib, pkg), os.path.join(proj, "src", pkg), ignore=shutil.ignore_patterns("__pycache__"))
with open(os.path.join(proj, "data", "customers.csv"), "w") as f:
    f.write("id,company,country,since\n")
    for i in range(1206):
        f.write(f"{i},Example Company {i:04d} Ltd,{['UK', 'NG', 'US', 'DE'][i % 4]},{2010 + i % 15}\n")
for i in range(12):
    with open(os.path.join(proj, "docs", f"chapter_{i + 1:02d}.md"), "w") as f:
        f.write(f"# Chapter {i + 1}\n\n" + "A paragraph the person wrote. " * 60 + "\n")
for i in range(12):
    with open(os.path.join(proj, "photos", f"IMG_{2041 + i}.jpg"), "wb") as f:
        f.write(os.urandom(60_000))
with open(os.path.join(proj, "notes.md"), "w") as f:
    f.write("# Notes\n\nIdeas I have not committed anywhere.\n" * 20)
with open(os.path.join(proj, "config.env"), "w") as f:
    f.write("DATABASE_URL=postgres://localhost/app\nFEATURE_FLAGS=checkout,search\n")
with open(os.path.join(root, ".bashrc"), "w") as f:
    f.write("alias ll='ls -la'\nexport EDITOR=vim\n")
print(f"a person's world at {root}: {sum(len(fs) for _, _, fs in os.walk(root))} files")
