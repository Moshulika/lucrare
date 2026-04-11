# Licenta Thesis Project

This repository contains the LaTeX source for a bachelor's thesis written in Romanian using the `teza-upb` document class.

## Files

- `teza.tex` — main thesis source file
- `teza-upb.cls` — custom UPB thesis class used by the document
- `licenta.bib` — bibliography database
- `figuri/` — figure assets
- `cod/` — code listings or scripts
- `cheatsheet/` — related supplementary document

## Build

Use a LaTeX build workflow that supports bibliography and glossary generation. A typical sequence is:

```bash
latexmk -pdf teza.tex
```

If you use `make`, you can also run:

```bash
make
```

## Abbreviations

The document uses a dedicated abbreviations section and can be configured with the `acro` package for automatic first-use expansion.

## Notes

- The thesis class is tailored for Politehnica University of Bucharest format.
- The document is currently in draft mode.
