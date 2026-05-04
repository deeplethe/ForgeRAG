"""
ForgeRAG evaluation harness — optional, opt-in.

The design is deliberately lightweight: a ``Dataset`` holds queries
(with ground-truth references), a ``RetrievalRun`` holds one run's
outputs, and ``metrics`` contains the scoring functions. Nothing
auto-runs in CI — scoring is expensive and corpora are user-specific.

Typical flow:

    from opencraig.eval import Dataset, RetrievalRun, metrics
    from opencraig.client import Client

    ds = Dataset.from_jsonl("my_eval.jsonl")
    c = Client("http://localhost:8000")

    run = RetrievalRun.execute(
        dataset=ds,
        retrieve=lambda q: c.ask(q.query, overrides={"allow_partial_failure": True}),
    )
    print("Recall@10:", metrics.recall_at_k(run, k=10))
    print("MRR:",        metrics.mrr(run))

LLM-judge scoring (for faithfulness / answer quality) lives in
``opencraig.eval.judge``.

Fixture corpus at ``tests/eval_fixtures/`` ships with the repo so the
ForgeRAG project itself can sanity-check its own retrieval on every
release. Users supply their own datasets for their own domains.
"""

from . import metrics
from .dataset import Dataset, EvalQuery, RetrievalRun
from .judge import LLMJudge

__all__ = [
    "Dataset",
    "EvalQuery",
    "LLMJudge",
    "RetrievalRun",
    "metrics",
]
