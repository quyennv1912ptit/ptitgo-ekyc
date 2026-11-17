# Phase 10.7 — Targeted OCR and parser reliability

## Evidence and scope

Phase 7.0 found a name proposal derived from a damaged English label even
though the recognizer read the actual value elsewhere. It also found a
partially emitted multi-class GPLX result and valid-looking wrong license
numbers. Phase 6.2 showed limitations upstream of parsing, including missed
text and ambiguous ground truth. Phase 6.5 rejected region heuristics.
Phase 6 rejected an exact-only no-expiry rule after expiry exact dropped
from 7/13 to 0/13. This phase uses a single targeted parser candidate and
the existing detector, OCR, grouping and Phase 6 DEV scorer.

## Changes

- The assisted runtime now distinguishes detector no-box, rejected
  geometry/keypoint confidence, rejected rectification, warp failure and
  quality rejection. The structured warnings accompany the existing
  raw-frame fallback. Expo and goAdmin show Vietnamese explanations.
  The strict CCCD policy continues using the existing preparation path and
  public error codes.
- QR/OCR name conflict detection compares Unicode NFC, case and spacing.
  Accents remain meaningful. The two source values are preserved. Numbers
  remain strings. Equivalent spelling formats are deduplicated in the
  demo and Expo instead of presenting a false source conflict.
- The experimental GPLX parser retains the legacy ID result byte for byte.
  It recognizes explicitly separated class codes only if every token is
  on the existing allowlist. It declines partial sets with unknown tokens.
  It isolates explicit issue, birth and expiry labels when a date candidate
  contains several labels. It suppresses ambiguous date/indefinite
  combinations while retaining the legacy anchored no-expiry recognition.
  A narrow English-label residue case can recover a unique nearby name.
  It abstains if multiple structurally plausible names remain. It does not
  guess accents or use OCR confidence to resolve those alternatives.

The legacy benchmark parser remains in place. The experimental parser is
**OFF** by default and blocked when ENV=prod. The local test opt-in is
`EKYC_EXPERIMENTAL_GPLX_ASSOCIATION=1`. Existing Hybrid C routing and model
weights were not used in this experiment.

## Paired DEV replay

Run `uv run python scripts/evaluate_gplx_phase10_7.py`. It checks the
historical hashes of the two Phase 6 inputs and reproduces that baseline
before reporting the candidate. Both runs consumed the same 13 cached OCR
documents with the same date and scorer. No OCR was rerun. Unreadable or
fully masked fields remain outside each metric's denominator. Missing here
means no emitted value, including an invalid candidate that was withheld.

| Field | Eligible | Exact baseline → candidate | Coverage baseline → candidate | Wrong emitted baseline → candidate | Missing baseline → candidate |
| --- | ---: | ---: | ---: | ---: | ---: |
| License number | 5 | 4 → 4 | 4 → 4 | 0 → 0 | 1 → 1 |
| Name, strict | 11 | 1 → 1 | 10 → 10 | 9 → 9 | 1 → 1 |
| DOB | 12 | 8 → 8 | 9 → 9 | 1 → 1 | 3 → 3 |
| Vehicle class | 13 | 10 → 10 | 10 → 10 | 0 → 0 | 3 → 3 |
| Expiry | 13 | 7 → 7 | 7 → 7 | 0 → 0 | 6 → 6 |
| Total | 54 | 30 → 30 | 40 → 40 | 10 → 10 | 14 → 14 |

There were **0 improvements, 0 regressions and 0 new wrong emissions** on
eligible fields. License number and class wrong emissions stayed at zero.
These are DEV cache results. The 54-field Phase 6 convention differs from
the 48-field later Phase 6 benchmark, and the Phase 7 exploratory set was
not included.

Synthetic regression tests directly reproduce the label-residue failure,
issue date borrowing, ambiguous expiry, loss of a multi-class set and
leading-zero preservation. They also cover name ambiguity and unknown
class abstention. These results justify retaining the experimental code for
targeted fixtures. The DEV replay does not justify enabling it. The
shipping changes are the diagnostic warnings and source comparison.

## Verification

No real document text appears in the runner output or this report. Phase 4
held-out, existing GT and the candidate lock were not altered or used for
tuning. The existing hard-case cache contains grouped lines, so the replay
cannot quantify detection, crop or raw association accuracy beyond those
inputs. Phase 10.7 ends after this replay and the synthetic regression suite.
