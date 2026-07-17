# Filter Benchmark v2 -- Report

Generated: 2026-07-17T22:08:04.965864+00:00  |  Rows: **1726**

## Per-Field Metrics

Coverage is data availability (did the ATS state it); precision/recall are extractor quality on top of that -- a field can have low coverage and still show perfect precision/recall on the rows it does have gold for.

| Field | N | Coverage | Precision | Recall | 95% CI (recall) |
|---|---:|---:|---:|---:|---|
| level | 1726 | 54.9% | 58.6% | 74.7% | [71.8%, 77.3%] |
| sector | 1726 | 98.4% | 84.0% | 61.2% | [58.8%, 63.5%] |
| country | 1726 | 97.6% | 99.7% | 85.6% | [83.9%, 87.2%] |
| city | 1726 | 82.9% | 88.8% | 93.9% | [92.6%, 95.0%] |
| remote | 1726 | 99.4% | 87.3% | 87.8% | [86.2%, 89.3%] |
| employment_type | 1726 | 45.2% | 31.9% | 35.4% | [32.1%, 38.8%] |
| salary | 1726 | 38.9% | 80.0% | 81.8% | [78.7%, 84.6%] |
| yoe | 1726 | 46.8% | 67.9% | 77.7% | [74.7%, 80.5%] |
| degree | 1726 | 37.0% | 74.9% | 93.9% | [91.8%, 95.5%] |
| sponsorship | 1726 | 3.5% | 93.6% | 73.3% | [61.0%, 82.9%] |
| posted_at | 1726 | 0.7% | 0.0% | 0.0% | [0.0%, 24.3%] |
| visa_sponsor | 1726 | 0.0% | 0.0% | 0.0% | [0.0%, 0.0%] |

## Provider x Field Matrix

The headline per-ATS diagnostic: which providers are dragging a field down, field by field.

### level

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 55.4% | 57.0% | 59.8% |
| dejobs | 518 | 60.2% | 51.5% | 80.1% |
| greenhouse | 181 | 41.4% | 50.8% | 88.0% |
| jazzhr | 154 | 44.2% | 66.2% | 75.0% |
| join | 71 | 53.5% | 70.0% | 55.3% |
| lever | 225 | 68.0% | 88.9% | 88.9% |
| personio | 203 | 46.3% | 48.7% | 60.6% |
| pinpoint | 12 | 33.3% | 57.1% | 100.0% |
| recruitee | 23 | 34.8% | 50.0% | 50.0% |
| teamtailor | 60 | 43.3% | 62.1% | 69.2% |
| workable | 95 | 71.6% | 61.5% | 58.8% |

### sector

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 91.8% | 64.1% | 29.6% |
| dejobs | 518 | 99.2% | 92.4% | 68.5% |
| greenhouse | 181 | 99.4% | 58.1% | 51.7% |
| jazzhr | 154 | 100.0% | 98.7% | 48.1% |
| join | 71 | 98.6% | 62.5% | 14.3% |
| lever | 225 | 100.0% | 91.1% | 86.2% |
| personio | 203 | 100.0% | 100.0% | 100.0% |
| pinpoint | 12 | 100.0% | 0.0% | 0.0% |
| recruitee | 23 | 100.0% | 100.0% | 69.6% |
| teamtailor | 60 | 100.0% | 0.0% | 0.0% |
| workable | 95 | 92.6% | 51.1% | 53.4% |

### country

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 97.8% | 100.0% | 85.0% |
| dejobs | 518 | 100.0% | 99.6% | 99.0% |
| greenhouse | 181 | 97.8% | 99.1% | 63.8% |
| jazzhr | 154 | 98.7% | 100.0% | 99.3% |
| join | 71 | 100.0% | 100.0% | 100.0% |
| lever | 225 | 97.3% | 100.0% | 94.1% |
| personio | 203 | 90.1% | 100.0% | 37.2% |
| pinpoint | 12 | 100.0% | 100.0% | 100.0% |
| recruitee | 23 | 82.6% | 100.0% | 36.8% |
| teamtailor | 60 | 98.3% | 98.2% | 91.5% |
| workable | 95 | 100.0% | 100.0% | 100.0% |

### city

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 29.9% | 59.3% | 92.7% |
| dejobs | 518 | 99.4% | 99.4% | 99.4% |
| greenhouse | 181 | 76.2% | 55.3% | 63.8% |
| jazzhr | 154 | 98.1% | 98.0% | 99.3% |
| join | 71 | 100.0% | 97.2% | 97.2% |
| lever | 225 | 68.0% | 88.4% | 99.3% |
| personio | 203 | 87.7% | 93.4% | 95.5% |
| pinpoint | 12 | 25.0% | 100.0% | 100.0% |
| recruitee | 23 | 82.6% | 82.6% | 100.0% |
| teamtailor | 60 | 98.3% | 83.1% | 83.1% |
| workable | 95 | 93.7% | 90.0% | 91.0% |

### remote

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 100.0% | 98.4% | 98.4% |
| dejobs | 518 | 100.0% | 100.0% | 100.0% |
| greenhouse | 181 | 94.5% | 71.8% | 76.0% |
| jazzhr | 154 | 100.0% | 95.5% | 95.5% |
| join | 71 | 100.0% | 80.3% | 80.3% |
| lever | 225 | 100.0% | 65.3% | 65.3% |
| personio | 203 | 100.0% | 86.2% | 86.2% |
| pinpoint | 12 | 100.0% | 75.0% | 75.0% |
| recruitee | 23 | 100.0% | 39.1% | 39.1% |
| teamtailor | 60 | 100.0% | 93.3% | 93.3% |
| workable | 95 | 100.0% | 82.1% | 82.1% |

### employment_type

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 91.8% | 90.2% | 98.2% |
| dejobs | 518 | 56.2% | 0.0% | 0.0% |
| greenhouse | 181 | 64.1% | 0.0% | 0.0% |
| jazzhr | 154 | 15.6% | 14.1% | 54.2% |
| join | 71 | 50.7% | 34.4% | 61.1% |
| lever | 225 | 10.7% | 6.1% | 54.2% |
| personio | 203 | 14.8% | 12.8% | 86.7% |
| pinpoint | 12 | 0.0% | 0.0% | 0.0% |
| recruitee | 23 | 13.0% | 13.0% | 100.0% |
| teamtailor | 60 | 58.3% | 0.0% | 0.0% |
| workable | 95 | 54.7% | 42.9% | 63.5% |

### salary

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 94.6% | 99.4% | 99.4% |
| dejobs | 518 | 30.5% | 30.4% | 32.9% |
| greenhouse | 181 | 63.5% | 93.1% | 93.9% |
| jazzhr | 154 | 24.0% | 100.0% | 97.3% |
| join | 71 | 42.3% | 96.3% | 86.7% |
| lever | 225 | 50.7% | 99.1% | 100.0% |
| personio | 203 | 1.0% | 40.0% | 100.0% |
| pinpoint | 12 | 0.0% | 0.0% | 0.0% |
| recruitee | 23 | 60.9% | 92.9% | 92.9% |
| teamtailor | 60 | 35.0% | 90.9% | 95.2% |
| workable | 95 | 6.3% | 100.0% | 83.3% |

### yoe

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 47.8% | 92.0% | 92.0% |
| dejobs | 518 | 82.4% | 61.6% | 65.3% |
| greenhouse | 181 | 51.9% | 79.8% | 92.6% |
| jazzhr | 154 | 46.1% | 94.2% | 91.5% |
| join | 71 | 21.1% | 45.0% | 60.0% |
| lever | 225 | 3.1% | 58.3% | 100.0% |
| personio | 203 | 15.8% | 33.3% | 100.0% |
| pinpoint | 12 | 0.0% | 0.0% | 0.0% |
| recruitee | 23 | 0.0% | 0.0% | 0.0% |
| teamtailor | 60 | 26.7% | 75.0% | 93.8% |
| workable | 95 | 61.1% | 91.4% | 91.4% |

### degree

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 14.1% | 83.9% | 100.0% |
| dejobs | 518 | 80.9% | 80.3% | 94.5% |
| greenhouse | 181 | 30.9% | 77.9% | 94.6% |
| jazzhr | 154 | 39.0% | 59.4% | 95.0% |
| join | 71 | 18.3% | 50.0% | 69.2% |
| lever | 225 | 0.0% | 0.0% | 0.0% |
| personio | 203 | 7.9% | 42.9% | 75.0% |
| pinpoint | 12 | 16.7% | 100.0% | 100.0% |
| recruitee | 23 | 0.0% | 0.0% | 0.0% |
| teamtailor | 60 | 5.0% | 16.7% | 66.7% |
| workable | 95 | 45.3% | 93.3% | 97.7% |

### sponsorship

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 0.0% | 0.0% | 0.0% |
| dejobs | 518 | 11.0% | 93.5% | 75.4% |
| greenhouse | 181 | 0.6% | 100.0% | 100.0% |
| jazzhr | 154 | 0.0% | 0.0% | 0.0% |
| join | 71 | 0.0% | 0.0% | 0.0% |
| lever | 225 | 0.0% | 0.0% | 0.0% |
| personio | 203 | 0.0% | 0.0% | 0.0% |
| pinpoint | 12 | 0.0% | 0.0% | 0.0% |
| recruitee | 23 | 0.0% | 0.0% | 0.0% |
| teamtailor | 60 | 0.0% | 0.0% | 0.0% |
| workable | 95 | 2.1% | 0.0% | 0.0% |

### posted_at

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 6.5% | 0.0% | 0.0% |
| dejobs | 518 | 0.0% | 0.0% | 0.0% |
| greenhouse | 181 | 0.0% | 0.0% | 0.0% |
| jazzhr | 154 | 0.0% | 0.0% | 0.0% |
| join | 71 | 0.0% | 0.0% | 0.0% |
| lever | 225 | 0.0% | 0.0% | 0.0% |
| personio | 203 | 0.0% | 0.0% | 0.0% |
| pinpoint | 12 | 0.0% | 0.0% | 0.0% |
| recruitee | 23 | 0.0% | 0.0% | 0.0% |
| teamtailor | 60 | 0.0% | 0.0% | 0.0% |
| workable | 95 | 0.0% | 0.0% | 0.0% |

### visa_sponsor

| Provider | N | Coverage | Precision | Recall |
|---|---:|---:|---:|---:|
| ashby | 184 | 0.0% | 0.0% | 0.0% |
| dejobs | 518 | 0.0% | 0.0% | 0.0% |
| greenhouse | 181 | 0.0% | 0.0% | 0.0% |
| jazzhr | 154 | 0.0% | 0.0% | 0.0% |
| join | 71 | 0.0% | 0.0% | 0.0% |
| lever | 225 | 0.0% | 0.0% | 0.0% |
| personio | 203 | 0.0% | 0.0% | 0.0% |
| pinpoint | 12 | 0.0% | 0.0% | 0.0% |
| recruitee | 23 | 0.0% | 0.0% | 0.0% |
| teamtailor | 60 | 0.0% | 0.0% | 0.0% |
| workable | 95 | 0.0% | 0.0% | 0.0% |

## Coverage vs. Precision

Sorted by coverage (ascending) so data-sparse fields are never mistaken for extractor misses -- a low-coverage / high-precision field means the ATS rarely states it, not that the extractor is bad at it.

| Field | Coverage | Precision | Gap (precision - coverage) |
|---|---:|---:|---:|
| visa_sponsor | 0.0% | 0.0% | 0.0% |
| posted_at | 0.7% | 0.0% | -0.7% |
| sponsorship | 3.5% | 93.6% | 90.1% |
| degree | 37.0% | 74.9% | 37.9% |
| salary | 38.9% | 80.0% | 41.2% |
| employment_type | 45.2% | 31.9% | -13.3% |
| yoe | 46.8% | 67.9% | 21.1% |
| level | 54.9% | 58.6% | 3.7% |
| city | 82.9% | 88.8% | 5.9% |
| country | 97.6% | 99.7% | 2.1% |
| sector | 98.4% | 84.0% | -14.4% |
| remote | 99.4% | 87.3% | -12.1% |

## Calibration

- Human-audited corrections: **0**
- Human-verified fraction: **0.0%** (how often the human's check matched what auto-resolution had already picked)
- Auto-accepted agreements checked: **0**
- False-agreement rate: **0.0%** (rate at which an extractor==fleet agreement was nonetheless wrong, measured on the audited slice)

## Worst Per-ATS Cells

The lowest-precision provider x field cells with at least 20 rows -- the first places to look for extraction bugs.

| Field | Provider | N | Precision | Coverage | Recall |
|---|---|---:|---:|---:|---:|
| employment_type | dejobs | 518 | 0.0% | 56.2% | 0.0% |
| posted_at | dejobs | 518 | 0.0% | 0.0% | 0.0% |
| visa_sponsor | dejobs | 518 | 0.0% | 0.0% | 0.0% |
| degree | lever | 225 | 0.0% | 0.0% | 0.0% |
| sponsorship | lever | 225 | 0.0% | 0.0% | 0.0% |
| posted_at | lever | 225 | 0.0% | 0.0% | 0.0% |
| visa_sponsor | lever | 225 | 0.0% | 0.0% | 0.0% |
| sponsorship | personio | 203 | 0.0% | 0.0% | 0.0% |
| posted_at | personio | 203 | 0.0% | 0.0% | 0.0% |
| visa_sponsor | personio | 203 | 0.0% | 0.0% | 0.0% |
| sponsorship | ashby | 184 | 0.0% | 0.0% | 0.0% |
| posted_at | ashby | 184 | 0.0% | 6.5% | 0.0% |
| visa_sponsor | ashby | 184 | 0.0% | 0.0% | 0.0% |
| employment_type | greenhouse | 181 | 0.0% | 64.1% | 0.0% |
| posted_at | greenhouse | 181 | 0.0% | 0.0% | 0.0% |
