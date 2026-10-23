"""Import only daytime images from a Colab run into the compact game pack.

Requires Pillow on the development machine, not on the player's device.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from PIL import Image

from background_lighting import fallback_definition, time_mode_for
from background_effects import effect_profile_for


def import_pack(source: Path, destination: Path, plan: Path | None = None) -> dict:
    if not (source / "locations.json").is_file():
        runs = sorted(source.glob("run_*/locations.json"))
        if len(runs) != 1:
            raise ValueError("Specify a single run folder containing locations.json")
        source = runs[0].parent
    locations = json.loads((source / "locations.json").read_text(encoding="utf-8-sig"))
    descriptions = {}
    if plan and plan.is_file():
        for line in plan.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"\|\s*\d+\s*\|\s*`([a-z_]+)`\s*\|\s*([^|]+)\|\s*([^|]+)\|", line)
            if match:
                descriptions[match[1]] = (match[2].strip(), match[3].strip())
    images = destination / "backgrounds"
    catalogs = destination / "visual_catalogs"
    images.mkdir(parents=True, exist_ok=True)
    catalogs.mkdir(parents=True, exist_ok=True)
    places, missing, source_bytes = {}, [], 0
    fallback = fallback_definition()
    for location in locations:
        asset_id = location["id"]
        is_fallback = bool(location.get("is_fallback")) or asset_id == "abstract_space"
        if not re.fullmatch(r"[a-z_]+", asset_id):
            raise ValueError("Invalid location ID")
        candidates = [source / "originals" / f"{asset_id}__day.png",
                      source / "backgrounds/realesrgan_x2" / f"{asset_id}__day.png",
                      source / "backgrounds/lanczos" / f"{asset_id}__day.png"]
        original = next((p for p in candidates if p.is_file()), None)
        if original is None:
            if not is_fallback:
                missing.append(asset_id)
            continue
        with Image.open(original) as image:
            image.load()
            if image.width * 9 != image.height * 16:
                raise ValueError(f"Expected 16:9: {original.name}")
            image = image.convert("RGB")
            if image.width > 1280:
                image = image.resize((1280, 720), Image.Resampling.LANCZOS)
            filename = f"{asset_id}__day.webp"
            target = images / filename
            image.save(target, format="WEBP", quality=92, method=6)
            width, height = image.size
        label, context = descriptions.get(asset_id, (location.get("label", asset_id), ""))
        entry = {
            "label": label, "prompt": location.get("description", ""),
            "keywords": [label, context], "pack": location.get("group", "universal"),
            "environment": location.get("lighting", "outdoor"), "worlds": [],
            "file": filename, "width": width, "height": height,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "time_mode": time_mode_for(location),
        }
        entry["effect_profile"] = effect_profile_for({**location, "sha256": entry["sha256"]})
        if is_fallback:
            fallback = {**fallback, **entry, "is_fallback": True, "time_mode": "fixed"}
        else:
            places[asset_id] = entry
        source_bytes += original.stat().st_size
    if not places:
        raise ValueError("No daytime images found; morning/night files are never used as substitutes")
    payload = {"schema_version": 4, "source_run": source.name, "places": places, "fallback": fallback}
    (catalogs / "background_catalog.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {"imported": len(places), "planned": sum(not (item.get("is_fallback") or item["id"] == "abstract_space") for item in locations), "missing": missing,
              "source_day_bytes": source_bytes,
              "fallback_ready": bool(fallback.get("file")),
              "game_pack_bytes": sum((images / p["file"]).stat().st_size for p in places.values()) + ((images / fallback["file"]).stat().st_size if fallback.get("file") else 0)}
    (catalogs / "background_import_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--plan", type=Path, default=Path(__file__).resolve().parents[1] / "BACKGROUND_PLAN.md")
    args = parser.parse_args()
    print(json.dumps(import_pack(args.source, args.destination, args.plan), ensure_ascii=False, indent=2))
