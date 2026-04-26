"""Quick sanity check: does the configured embedder bridge zh↔en?

Runs `cos(embed("蜜蜂"), embed("bee"))` and a couple more pairs.
Prints the scores so you can see at a glance whether the embedder is
multilingual.

Usage:
    python scripts/test_crosslingual_embed.py
    python scripts/test_crosslingual_embed.py myconfig.yaml
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# Make project root importable regardless of where this is run from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from embedder.base import make_embedder
from persistence.store import Store


def cos(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


def main() -> None:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else None
    cfg = load_config(cfg_path)

    # Each module owns its model/api_key/api_base inline — no startup
    # indirection. We still touch the store so DB-side state can be
    # inspected from the same process.
    store = Store(cfg.persistence.relational)
    store.connect()
    store.ensure_schema()

    emb_cfg = cfg.embedder
    inner = getattr(emb_cfg, "litellm", None)
    print(f"Embedder backend : {emb_cfg.backend}")
    print(f"Embedder model   : {getattr(inner, 'model', '-')}")
    print(f"API base         : {getattr(inner, 'api_base', '-')}")
    print(f"API key present  : {bool(getattr(inner, 'api_key', None))}")
    print()

    emb = make_embedder(emb_cfg)

    pairs = [
        ("蜜蜂", "bee"),
        ("养蜂人", "beekeeper"),
        ("养蜂人与蜜蜂的关系", "relationship between beekeepers and bees"),
        # Control pair: unrelated words should score low.
        ("蜜蜂", "car"),
    ]
    texts = list({t for pair in pairs for t in pair})
    vecs = emb.embed_texts(texts)
    vec_of = dict(zip(texts, vecs, strict=True))

    print(f"{'pair':<55}  cos")
    print("-" * 70)
    for a, b in pairs:
        score = cos(vec_of[a], vec_of[b])
        print(f"{a!r:<25} vs {b!r:<25}  {score:+.3f}")

    print()
    zh_bee_en_bee = cos(vec_of["蜜蜂"], vec_of["bee"])
    zh_bee_car = cos(vec_of["蜜蜂"], vec_of["car"])
    signal = zh_bee_en_bee - zh_bee_car
    print(f"signal (bee-pair minus control) = {signal:+.3f}")
    if zh_bee_en_bee > 0.6:
        verdict = "✅ multilingual — KG can be rewired to use entity-name embeddings"
    elif zh_bee_en_bee > 0.3:
        verdict = "⚠️  weak — may work with tuning, but a real multilingual model is safer"
    else:
        verdict = "❌ monolingual — switch embedder before wiring cross-lingual KG"
    print(f"verdict: {verdict}")


if __name__ == "__main__":
    main()
