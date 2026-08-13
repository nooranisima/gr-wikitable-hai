"""Convert the Colab/Drive pool (wtq_pool_with_synthetic.json) into the app's
pool_data.json, validating that every item has all three representations.

Usage: python scripts/make_pool_data.py /path/to/wtq_pool_with_synthetic.json
"""
import json, sys
from pathlib import Path

src = Path(sys.argv[1])
raw = json.load(open(src))
items = raw["items"] if isinstance(raw, dict) else raw
REPS = ["code", "strategy", "goal_means"]
ok, dropped = [], []
for it in items:
    reps = it.get("representations") or {}
    if all(reps.get(x) for x in REPS):
        ok.append(it)
    else:
        dropped.append((it.get("id"), [x for x in REPS if not reps.get(x)]))
out = Path(__file__).parent.parent / "pool_data.json"
json.dump({"meta": {**(raw.get("meta", {}) if isinstance(raw, dict) else {}),
                    "placeholder": False, "source": src.name},
           "items": ok}, open(out, "w"), ensure_ascii=False, indent=1)
n_cor = sum(1 for it in ok if it["is_correct"])
n_syn = sum(1 for it in ok if it.get("synthetic"))
print(f"wrote {out}: {len(ok)} items ({n_cor} correct / {len(ok)-n_cor} incorrect, "
      f"{n_syn} synthetic) -> {len(ok)*3} pairings")
for d in dropped:
    print("  dropped (missing reps):", d)
