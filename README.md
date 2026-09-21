# movable-type

> **Status: idea stage.** Requirements only — no implementation yet.

A rendering engine that turns **any information source** (RSS / JSON / Markdown) into a
**living old-newspaper page**: print-style layout, motion in the pictures, and text that
scatters apart and reassembles when you turn the page.

Not an aggregator. Aggregation is a solved problem (`goosepaper`, `unim.press`,
`RSSPub`, …). The unsolved part is making the **page itself alive**.

---

## Why this exists

Measured before writing anything:

| Question | Finding |
|---|---|
| Is the niche taken? | The "RSS → newspaper layout" space is **crowded** (342★ / 249★ / 132★ / 131★ projects). Every "Harry Potter magic newspaper" implementation on GitHub is a **toy — all 0–1★**. |
| The gap | **"A living page fed by real information sources" has no serious implementation.** Closest is a 1★ Reddit-only script. |
| Do sources come with motion? | Only **36%** of open-source READMEs ship a GIF/video; **71%** have a still image. So most motion has to be generated. |
| Can we generate it locally? | **Yes.** DynamiCrafter (2.9 GB) fits an 8 GB laptop GPU and is built for animating stills. |

---

## The name

**movable-type** — movable type is the ancestor of typesetting (information → page),
and the phrase literally means *"characters that move"*, which is exactly what the
page-turn effect does.

Hyphenated deliberately, to stay distinct from the legacy `movabletype` CMS.

---

## Read the requirements

→ [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md)

It covers the layout model, the motion tiers, the three constraints we measured
(per-character positioning is cheap; animations corrupt text nodes; element rect reads
lie while animating), and the page-turn text morph design.

## Prior art / related work

- [`j6k4m8/goosepaper`](https://github.com/j6k4m8/goosepaper) — daily newspaper to reMarkable
- [`thesephist/unim.press`](https://github.com/thesephist/unim.press) — Reddit front page as NYT
- [`MatthK/Selfoss-Webfront`](https://github.com/MatthK/Selfoss-Webfront) — newspaper-style RSS frontend
- [`harshit181/RSSPub`](https://github.com/harshit181/RSSPub) — RSS to a personal daily paper

## License

TBD.
