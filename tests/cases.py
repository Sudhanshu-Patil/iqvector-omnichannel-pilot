import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SPEC = json.loads((DATA_DIR / "03_contracts_and_30_eval_cases.json").read_text(encoding="utf-8"))
EVAL_CASES = SPEC["eval_30_cases"]
CASES = {case["case_id"]: case for case in EVAL_CASES}
