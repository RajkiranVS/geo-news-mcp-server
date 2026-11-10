# Test fixtures

`bbc_world.xml`, `hindu_international.xml` and `hindu_national.xml` are real feed
responses captured on 2026-11-10. They are frozen: tests assert exact counts
derived from them, including

- items per feed: 33 (bbc_world), 60 (hindu_international), 60 (hindu_national)
- NVIDIA keyword matches: 12 total (4 bbc_world, 8 hindu_international)
- the newest-item timestamp used by the ordering test

**If you recapture these fixtures, update those counts in `tests/test_feeds.py`**
(search for `33`, `60`, `12`). The other files (`empty_valid_feed.xml`,
`encoding_override.xml`, `html_error_page.html`, `truncated_bbc.xml`) are
hand-made for specific failure cases.
