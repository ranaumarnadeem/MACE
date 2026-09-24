# Writing rules

These rules apply to everything written in this repository: docs pages, the paper, README files, pull request descriptions, commit messages, and code comments.
The paper and the docs site were written and revised under them.

## Tone

- Write short, plain sentences in the active voice, one fact per sentence.
- Say a thing once. Don't repeat a word within a few sentences or a sentence across pages; link to the page that already says it.
- Leave out these words and phrases: real, actual, actually, genuine, genuinely, honest, honestly, truly, leverage, utilize, seamless, robust, comprehensive, crucial, delve, showcase, moreover, furthermore, additionally, overall, various, numerous, "in order to", "note that", "it is worth noting", "importantly", "state-of-the-art", and "key" in the sense of important.
- Leave out defensive phrasing, such as "we do not claim" or "to be clear".
- Don't use em dashes, "not X, but Y" sentences, or bold for emphasis inside a sentence.
- Write "Section 4" instead of "§4".

## Accuracy

- Check every claim against the code before writing it.
- Take numbers from the run records or the paper's Table 1, and keep each number the same everywhere it appears.
- In the docs and the paper:
  - report results for the 2x2 and 4x4 meshes only;
  - describe how the code behaves now, and leave out bugs that were fixed during deployment.

## Docs pages

- A page starts with `% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran`, a blank line, and `# Title`. Sphinx needs the title to link the page.
- Link to pages, never to `#anchor` targets. The docs build can't resolve them and fails.
- Pages are Markdown, read through MyST. Each part's table of contents is its `index.rst`.
- Before opening a pull request, build with `make -C docs SPHINXOPTS="-W --keep-going"`. CI fails on any warning.

## Paper

- The paper uses the IEEEtran class and stays within 4 pages.
- The text refers to every figure and table.
- Limitations and Future Directions stay brief, as two subsections.
