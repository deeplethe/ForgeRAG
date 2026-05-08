# Eval Fixtures

Tiny synthetic datasets we use to sanity-check retrieval quality in
OpenCraig's own CI / pre-release runs. They are **not** a benchmark —
they're tripwires for catching regressions in the retrieval pipeline.

## Files

* `smoke_queries.jsonl` — 5 queries with `relevant_chunk_ids`
  pre-recorded against a fixed in-memory corpus. A run with
  `recall@5 < 0.8` means something in the retrieval layer regressed.

## Writing your own dataset

Each line is one query in this shape (fields match
`forgerag.eval.EvalQuery`):

```json
{
  "query_id": "q_001",
  "query": "What is X?",
  "relevant_chunk_ids": ["chunk_abc", "chunk_def"],
  "relevant_doc_ids": ["doc_1"],
  "expected_answer": "X is Y.",
  "tags": ["definition", "single_hop"]
}
```

Load with:

```python
from opencraig.eval import Dataset
ds = Dataset.from_jsonl("my_eval.jsonl")
```

Run it against a deployed server:

```python
from opencraig.client import Client
from opencraig.eval import RetrievalRun, metrics

c = Client("http://localhost:8000")
run = RetrievalRun.execute(
    ds,
    retrieve=lambda q: c.ask(q.query, overrides={"allow_partial_failure": True}),
)
print(metrics.summary(run, k=10))
```

For LLM-judged faithfulness / context-precision see
`forgerag.eval.LLMJudge`.
