# seedbank catalog

The public seedbank catalog. One row per seed under `rows/<owner>/<name>.json`.

A row looks like:

```
{
  "name": "<owner>/<seedname>",
  "link": "<host>/<repo-owner>/<repo>[//<subfolder>]",
  "commit": "<sha reviewed>",
  "tree":   "<tree of commit:subfolder>",
  "lane":   "A" | "B" | "C" | "D",
  ...fields from seed.json...
}
```

Submit a row by running `seedbank publish` in your seed. The CLI opens a PR here. CI reads the row, fetches the pinned
commit (never runs it), scans it, picks a lane, and auto-merges lanes A and B.

See [github.com/etiennedoidic/seedbank](https://github.com/etiennedoidic/seedbank) for the CLI and
[docs/seedbank-review.md](https://github.com/etiennedoidic/seedbank/blob/main/docs/seedbank-review.md) for the design.
