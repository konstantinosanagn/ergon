# Search and incremental-update correctness evaluation

This evaluation tests explicit behavioral contracts, rather than assigning an unsupported overall relevance score. The baseline is commit `8e6cdd52fbd4ca54d59a5c29a8f4db20edf5d435`.

## Criteria and results

The initial 14-case offline evaluation contained 12 judged retrieval cases, a change-feed identifier case, and an incompatible-model case. The baseline passed 6 and failed 8. The revised implementation passes all 14. Additional regressions cover alias expansion crossing the five-token threshold, missing model metadata, dimension mismatch, and both incremental vector writers rejecting incompatible models without mutating the sidecar.

The judgments are intentionally narrow: explicit C++ and C# searches must distinguish those languages before applying a result limit; opt-in semantic candidate retrieval must recognize ML/machine learning in either direction; hard country filters must still hold; lexical-only ML behavior stays unchanged; ordinary, Unicode, punctuation-only, and escaped-operator queries retain their behavior. Each fixture has a distinct employer that does not contain its query terms. Job identities are checked through provenance, not the index adapter's synthetic source_job_id.

Run the evaluation with:

```bash
python -m pytest tests/test_search_adversarial_eval.py tests/test_content_revalidation.py -q
```

This is a regression acceptance suite, not a statistically representative relevance benchmark. It makes no claim about overall recall, nDCG, multilingual embedding quality, or user satisfaction. The fake embedder tests compatibility/control flow, not semantic model accuracy.

## Published-snapshot smoke comparison

A local comparison used the September 10, 2026 Crypto/Web3 shard, build `build-2026-09-10-188`, containing 2,511 rows. Its decompressed SHA-256 was verified against the published shard manifest. This small shard was chosen for download size, not as a representative industry sample.

Both versions queried the same local SQLite file with `semantic=True`, `limit=5`, through `search_rows`. No model inference, live ATS requests, or remote query endpoint was involved. Twenty sequential calls per query on Windows/Python 3.13 gave the following illustrative medians:

| Query | Baseline candidates | Revised candidates | Baseline / revised median ms |
| --- | --- | --- | --- |
| ML engineer | None | Five Machine Learning Engineer titles | 0.326 / 0.983 |
| C++ | Four unrelated E&C/C-IMP titles and one C/C++ role | Three explicit C++ titles | 0.551 / 0.797 |
| C# | Same five results as C++ | None in the searchable fields | 0.540 / 0.695 |
| Rust | Five Rust titles | Same five, same order | 0.468 / 0.487 |

These timings exclude download, deserialization into JobPosting, vector loading, model inference, and full-corpus scale. The explicit identifier predicate adds work to the FTS candidate scan; measure large-corpus C-token queries before claiming a latency improvement. A missing C# candidate says nothing about requirements outside the stored snippet.

## Update correctness

`last_crawled` already advances when processing outcomes, including unchanged membership. It cannot establish content freshness. The new `last_content_crawled` field advances only after a successful crawl with complete normalization. Membership-only skips do not extend it. Missing, malformed, future, or seven-day-old content timestamps require revalidation when the board next enters the normal crawl window. Recent matching membership can still skip. Transient fetch failures do not refresh the timestamp.

This is a bound on skip eligibility, not a seven-day delivery SLA: crawl windows, scheduler backoff, unavailable providers, and host budgets still apply. Legacy state safely loads with an unknown content timestamp and revalidates within the existing bounded crawl process. It may increase crawl work during rollout, but does not raise any rate limit or budget. Whole-body conditional validators retain their normal conditional-GET path; a membership-only validator cannot substitute for a due content revalidation.

Both incremental embedding writers now reject unknown/different model identities before modifying a populated sidecar. Query-time use checks model and dimension and falls back to the existing reranking path on incompatibility. Changing a model requires explicitly building a separate sidecar; the PR does not silently trigger a corpus-wide re-embed. Model-name equality is still not a cryptographic model-revision or per-document-content guarantee.

## Deliberately unresolved evaluation failures

- The 300-character core snippet and 600-character fresh embedding representation can omit substantive requirements. A section-aware representation needs its own versioned migration and judged evaluation.
- Semantic retrieval remains lexical candidate generation plus vector reranking. The bounded ML alias addresses one demonstrated recall failure; it is not independent dense retrieval or hybrid fusion.
- The existing four-to-five-token query policy remains. Alias expansion is prevented from accidentally crossing that boundary.
- Sector classification, full-JD coverage, stale per-job vector signatures, publication generation compatibility, and full-corpus cold-start capacity need broader evaluation.

For a production relevance decision, assemble held-out judgments from actual target roles/locations, measure candidate Recall@100 separately from nDCG@10, and compare lexical, alias-expanded, section-aware, and independent hybrid retrieval under identical hard filters. Report storage, model revision, p95 latency, and peak memory with those results.
