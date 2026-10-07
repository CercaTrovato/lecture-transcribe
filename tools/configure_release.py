"""Configure model-free online package URLs; no publishing or git operations."""
import argparse
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--base-url", required=True)
parser.add_argument("--app-package-url", help="Application archive directory URL; defaults to base-url/desktop/nsis-web/")
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--unsigned-prototype", action="store_true")
args = parser.parse_args()
base = args.base_url.rstrip("/") + "/"
if not base.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
    raise SystemExit("Use HTTPS, or a loopback HTTP address for local verification")
manifest_path = root / "release" / "components.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
manifest["base_url"] = base
manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
package = json.loads((root / "desktop" / "package.json").read_text(encoding="utf-8"))
config = package["build"]
config["extraResources"].append({"from": str(manifest_path), "to": "components.json"})
app_url = (args.app_package_url or base + "desktop/nsis-web/").rstrip("/")
if not app_url.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
    raise SystemExit("Use HTTPS, or a loopback HTTP address for the application package")
config["nsisWeb"]["appPackageUrl"] = app_url
if args.unsigned_prototype:
    config["win"]["signAndEditExecutable"] = False
config["electronDist"] = str(root / "desktop" / "node_modules" / "electron" / "dist")
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(config, indent=2), encoding="utf-8")
print(args.output)
