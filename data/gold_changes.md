# Gold label and metric changes

Every change made to evaluation labels or to the retrieval metric AFTER seeing
results is logged here, with the reason, so the evaluation stays honest.
Baseline run before any change: `results/retrieval_eval/retrieval_eval_20261112T173235Z.json`
(single-chunk evidence metric, original gold).

Conventions: `pages` = 1-based PHYSICAL PDF page index (position in the file).
`printed_pages` = number typeset on the page. They are stored and matched separately.

## 2026-11-12

### Metric: evidence is judged on the retrieved set, not one chunk
Reason: the generator sees all k retrieved chunks. Requiring every evidence phrase
inside one chunk penalised small chunks that split adjacent sentences (q03). The old
test is still reported as `strict%`. Not a gold change; applies to all questions and settings.

### Metric: spacing-insensitive fallback for long evidence phrases
Reason: extraction re-joins words split at a line break ("position-" + "wise" became
"positionwise"), so q14's phrase "position-wise fully connected feed-forward network" could never
match the extracted text although the paper contains it. For phrases of 12+ characters (spaces
removed), matching falls back to a spacing-insensitive comparison; shorter phrases still need whole
words. No gold label changed. The retrieval tokenizer also now emits the joined form of hyphenated
words, so "position-wise" and "positionwise" match each other (a retrieval change, measured by the
next eval run).

### q10: alternative location added (group `mimi_definition`)
- Original entry kept: Moshi physical p.3 (introduction bullet defining Mimi, with RVQ).
- Added: Moshi physical pp.9-10, Section 3.3 "Audio Tokenization".
- Why justified: read directly from the extracted text of those pages. It supports both
  required facts independently of page 3:
  - Mimi is a neural audio codec: p.9, "we introduce Mimi, a neural audio codec ... that
    operates as an autoencoder with a discrete bottleneck".
  - It uses RVQ: p.10, "We discretize the latent space with a Residual Vector Quantizer
    ..., which iteratively applies vector quantization (VQ) to the residuals of the previous quantizer."
- Risk acknowledged: the alternative was chosen after seeing that retrieval returned this section
  (the first eval counted that as a miss). It is accepted only because the section answers the
  question by itself, not because the retriever found it.

### q04: gold page corrected from 7 to 6 (factual correction)
- The original gold said physical page 7. The extracted data put "3.5 Positional Encoding" and both
  evidence phrases on physical page 6 (page 7 begins "length n is smaller than the representation
  dimensionality d"). Table 1 (q06, gold p.6) is on the same extracted page as 3.5.
- Resolved by the author checking the PDF directly: the section is on page 6. The label was a
  page-numbering slip in the original gold, not a retrieval or extraction problem.
- Why this is not tuning: the correction comes from the paper, not from the retriever's output.
  The pre-correction baseline run is kept for comparison.
