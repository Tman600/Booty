# Builds out/Booty.zip in the layout Decky's "Install Plugin from ZIP" expects.
import os, zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = ["plugin.json", "package.json", "main.py", "LICENSE", "README.md", "dist/index.js"]
os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
out = os.path.join(ROOT, "out", "Booty.zip")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in FILES:
        z.write(os.path.join(ROOT, f), "Booty/" + f)
print(out)
