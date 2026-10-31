from __future__ import annotations

import argparse
import json
from pathlib import Path
import yaml

from backend.data_pipeline.bundle_store import save_bundle
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.data_pipeline.quality import validate_bundle
from backend.data_pipeline.storage import write_json_atomic, write_ndjson_atomic


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize the explicit Phase 1 synthetic data bundle"
    )
    parser.add_argument("--root", default="data")
    parser.add_argument("--report", default="reports/phase1/fixture_validation.json")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    bundle = build_phase1_fixture()
    paths = save_bundle(Path(args.root), bundle)
    config_path = Path(args.config)
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        cost_path = Path(args.root) / "meta" / "cost_schedules.ndjson"
        write_ndjson_atomic(
            cost_path,
            [
                {
                    "scenario": "base",
                    **config["cost"],
                    "source_kind": "PARAMETERIZED_NOT_BROKER_DOCUMENT",
                }
            ],
        )
        paths["cost_schedules"] = cost_path
    report = validate_bundle(bundle)
    write_json_atomic(Path(args.report), report.to_dict())
    print(
        json.dumps(
            {
                "source_kind": bundle.manifest_data.source_kind,
                "data_version": bundle.manifest_data.data_version,
                "codes": list(bundle.dataset.codes),
                "paths": {key: str(value) for key, value in paths.items()},
                "quality": report.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

