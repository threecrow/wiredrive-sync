#!/usr/bin/env python3
from pathlib import Path
import shutil, plistlib, os

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "app"
PKG = ROOT / "packaging" / "macos"
OUT = ROOT / "dist" / "Wiredrive Sync.app"

if OUT.exists():
    shutil.rmtree(OUT)

contents = OUT / "Contents"
macos = contents / "MacOS"
resources = contents / "Resources"
payload = resources / "WiredriveSync"

payload.mkdir(parents=True)
macos.mkdir(parents=True)

for p in SRC.iterdir():
    if p.name == "__pycache__":
        continue
    dest = payload / p.name
    if p.is_dir():
        shutil.copytree(p, dest)
    else:
        shutil.copy2(p, dest)

shutil.copy2(PKG / "WiredriveSync", macos / "WiredriveSync")
shutil.copy2(PKG / "Info.plist", contents / "Info.plist")
shutil.copy2(PKG / "AppIcon.icns", resources / "AppIcon.icns")

(macos / "WiredriveSync").chmod(0o755)
if (payload / "macos_launcher.py").exists():
    (payload / "macos_launcher.py").chmod(0o755)

print(f"Built: {OUT}")
