# Buy or Wait? — Final Full-Dataset LLM Usage Report

This report documents all model usage, token consumption, provenance, and estimated costs for producing the final evaluation predictions for all 250 requests in `dataset/requests.csv`.

## 1. Executive Summary

- **Total Evaluation Requests**: 250
- **New Model Calls During Final Run**: 0
- **Evidence Extraction Cache Reuse**: Reused verified local aggregate (`code/data/extracted_deltas.json`: 16 images, 230 deltas covering 215 messages)
- **Earlier Evidence Calls Recorded in Provenance Log**: 130
- **Excluded Test / Probe Calls**: 11
- **Total Input Tokens (Evidence)**: 372,699
- **Total Output Tokens (Evidence)**: 52,112
- **Total Tokens Consumed (Evidence)**: 424,811
- **Average Input Tokens Per Request**: 1490.80
- **Average Output Tokens Per Request**: 208.45
- **Average Total Tokens Per Request**: 1699.24
- **Estimated Total Evidence Extraction Cost**: $0.0490
- **Estimated Cost Per Evaluation Request**: $0.000196

## 2. Model Breakdown & Token Consumption (Evidence Extraction)

| Provider | Model | Call Type | Calls | Input Tokens | Output Tokens | Total Tokens | Est. Cost (USD) |
|---|---|---|---|---|---|---|---|
| google | `gemini-3.1-flash-lite` | `image_extraction` | 1 | 1,702 | 73 | 1,775 | $0.0001 |
| google | `gemini-3.1-flash-lite` | `message_batch` | 25 | 232,235 | 31,237 | 263,472 | $0.0268 |
| google | `gemini-3.1-flash-lite` | `message_extraction` | 86 | 109,508 | 10,028 | 119,536 | $0.0112 |
| google | `gemini-3.6-flash`* | `image_extraction` | 15 | 25,418 | 7,698 | 33,116 | $0.0084 |
| google | `gemini-3.6-flash`* | `message_extraction` | 3 | 3,836 | 3,076 | 6,912 | $0.0024 |
| **Total Evidence** | | | **130** | **372,699** | **52,112** | **424,811** | **$0.0490** |

\* *Indicates model with proxy pricing estimate where official standard rate card is unavailable (see §3).* 

### Excluded Test / Probe Calls

The following calls were non-production diagnostic or test harness invocations and are excluded from final evidence extraction totals:

| Provider | Model | Call Type | Calls | Input Tokens | Output Tokens | Total Tokens | Notes |
|---|---|---|---|---|---|---|---|
| google | `gemini-3.1-flash-lite` | `test` | 11 | 45 | 12 | 57 | Pre-run test harness probes |

## 3. Pricing Assumptions & Methodology

- **Official Pricing Reference**: [https://ai.google.dev/pricing](https://ai.google.dev/pricing)
- **Pricing Date / Standard Tier**: Rates applicable for standard paid API usage.
- **`gemini-3.1-flash-lite`**: Official standard rate is **$0.075 / 1,000,000 input tokens** ($0.000000075/token) and **$0.30 / 1,000,000 output tokens** ($0.00000030/token).
- **`gemini-3.6-flash`**: Official public rate card price is **Unavailable**. An explicit proxy rate of **$0.15 / 1,000,000 input tokens** and **$0.60 / 1,000,000 output tokens** is applied based on the standard multimodal Gemini Flash tier assumption.
- **Silent Default Pricing**: Explicitly removed and disallowed. Any model without documented official or proxy rates causes a fail-closed exception.
- **Cost Derivation**: Calculated exactly as `(input_tokens / 1,000,000) * input_rate + (output_tokens / 1,000,000) * output_rate` without rounding before final sum.

## 4. Cache & Reproducibility Invariant

- LLM evidence extractions are saved in `code/cache/` by prompt version and aggregated into `code/data/extracted_deltas.json`.
- Cached evidence makes repeated pipeline runs reproducible across development and production environments.
- During the final full evaluation run, 0 new API calls were made; 100% of required evidence was verified and loaded from local cache.
- No API keys, credentials, secrets, or sensitive PII are recorded in this report or committed to the repository.
