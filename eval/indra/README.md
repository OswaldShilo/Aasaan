# Indra OCR spike

Question: should Intake use Indra OCR (`datalab-to/indra`, `indra-ocr` on PyPI) as tiers A and B? Decision rule (Milestone 4, section 3): adopt only if page coverage is at
least as good as the current path, median time per page is lower, heading and contents detection is not worse, and nothing regresses. Result and numbers: `REPORT.md`.

Reproduce (Docker only; Indra is Linux/macOS, not native Windows):

```bash
python eval/indra/make_fixtures.py                              # builds eval/indra/fixtures (git-ignored)
docker build -t aasaan-indra-spike eval/indra                   # pins indra-ocr 0.3.2, bakes the models into the image
docker run --rm --network none -v "$PWD/eval/indra/fixtures:/in:ro" -v "$PWD/eval/indra/out:/out" aasaan-indra-spike   # no network needed at run time
python eval/indra/run_current.py                                # the current path (live vision model for pages with no text layer)
python eval/indra/compare.py                                    # scores both with the same code
```

Files: `make_fixtures.py` (generated fixtures, nothing copyrighted), `scoring.py` (ground truth and metrics), `run_indra.py` (inside the container),
`run_current.py` (current path), `compare.py`, `prefetch.py` + `Dockerfile` (image with models baked in).
