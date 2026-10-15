# ADR-0003: One primary extractor per route; second parser only on triggers

**Status**: Proposed · **Date**: 2026-10-14

## Context
The unified stack listed two or three tools per format with no rule for when each runs. Double-processing every clean file doubles cost and time without improving the denominator.

## Decision
Each route (HTML source, rendered DOM, PDF native, PDF tables, image OCR, Office) has one primary extractor and a declared list of **triggers** (garbled ratio, empty page with image objects, order ambiguity, disagreement with render, parse warnings, low OCR confidence) that invoke the second tool. The visual text-region detector always runs on renders because it is the independent channel that finds what native extraction missed. Cloud OCR is a FINANCIAL route invoked only by trigger and only within the run's cost ceiling.

## Consequences
- Cost and latency scale with difficulty, not with volume.
- Every second-opinion invocation is recorded as an attempt with its trigger, so reviewers can see why it ran.
- The adapter registry returns `UNSUPPORTED` for anything unmapped; support grows by fixture.
