# Phase 1 Reactivation Pilot — UTM Tagging Scheme

**Window:** 2026-05-23 → 2026-08-21 (90 days)
**Owner:** Brian (posts), Edison (measurement)
**Why this exists:** so every organic post Brian publishes can be attributed
back to a platform + a specific piece of content in GA4 and our `business`
Postgres warehouse.

## Scheme spec

Every link Brian posts to `eldersninerelics.com` **or** to an Etsy listing
during Phase 1 must be tagged with the four standard UTM parameters below.

```
?utm_source=<platform>
&utm_medium=organic
&utm_campaign=phase1_reactivation
&utm_content=<post_identifier>
```

| Param          | Allowed values                                                                 | Notes |
|----------------|--------------------------------------------------------------------------------|-------|
| `utm_source`   | `facebook` \| `instagram` \| `pinterest` \| `reddit` \| `discord` \| `bgg`     | Lowercase, no spaces. One value per post. |
| `utm_medium`   | `organic` (fixed)                                                              | Phase 2 paid pushes will use `cpc` or `paid_social`, so we can isolate by medium later. |
| `utm_campaign` | `phase1_reactivation` (fixed)                                                  | This is what isolates the 90-day pilot from earlier or future activity. |
| `utm_content`  | Free-text, snake_case, ≤ 50 chars                                              | Use a stable identifier per post — e.g. `fearTracker_processVid_001`, `bbeg_painting_carousel`. Re-using the same value across cross-posts on different platforms is fine and lets you compare same-content performance across `utm_source` values. |

### Naming conventions (small but they matter)

- **All lowercase.** GA4 treats `Facebook` and `facebook` as different sources.
- **No spaces.** Use underscores. Spaces in URLs become `%20` and look broken.
- **Don't tag Etsy listing URLs that you don't post.** UTM tagging only
  applies to links Brian *posts on his own profiles or community accounts*.
  Etsy's own search/internal placements are not under our control and should
  not be tagged.
- **Same `utm_content` across platforms** when you cross-post the same asset.
  This is what lets us answer "did the painting video work better on IG or
  Reddit?".

## Allowed `utm_source` values

| Source        | When to use                                  |
|---------------|----------------------------------------------|
| `facebook`    | Posts on the FB business page or in groups   |
| `instagram`   | IG feed posts, reels, stories with link sticker |
| `pinterest`   | Pinterest pins                                |
| `reddit`      | r/daggerheart or other subreddit posts        |
| `discord`     | Daggerheart Discord links                     |
| `bgg`         | BoardGameGeek Marketplace listings / forum posts |

If you start posting somewhere new during Phase 1, add the source to this doc
*before* using it, so we don't end up with `tiktok` and `tik_tok` in the same
dataset.

## Examples

Brian posts a Fear Tracker process video to Instagram, with link in bio:

```
https://www.etsy.com/listing/1234567890/fear-tracker-daggerheart?utm_source=instagram&utm_medium=organic&utm_campaign=phase1_reactivation&utm_content=fearTracker_processVid_001
```

Same video, cross-posted as a Reddit submission on r/daggerheart:

```
https://www.etsy.com/listing/1234567890/fear-tracker-daggerheart?utm_source=reddit&utm_medium=organic&utm_campaign=phase1_reactivation&utm_content=fearTracker_processVid_001
```

A Pinterest pin linking to the homepage:

```
https://eldersninerelics.com/?utm_source=pinterest&utm_medium=organic&utm_campaign=phase1_reactivation&utm_content=storefront_pin_may01
```

A BGG Marketplace forum post linking to a specific listing:

```
https://www.etsy.com/listing/9876543210/bbeg-tracker?utm_source=bgg&utm_medium=organic&utm_campaign=phase1_reactivation&utm_content=bbeg_bgg_thread_intro
```

## Where this shows up downstream

GA4's BigQuery export writes UTM params into the `event_params` array under
the keys `source`, `medium`, `campaign`, `content`. Our loader
(`scripts/load_ga4.py`) already promotes the first three into dedicated
columns on `raw.ga4_events`:

- `collected_traffic_source_source`
- `collected_traffic_source_medium`
- `collected_traffic_source_campaign`

`utm_content` is **not** promoted to its own column — it's still readable via
`event_params` jsonb. The Phase 1 progress query
(`scripts/queries/phase1_progress.sql`) unnests it on demand.

## Helper script

`scripts/utm.py` wraps a base URL with the right UTM string so you don't have
to assemble it by hand:

```bash
./venv/bin/python scripts/utm.py \
  --url https://www.etsy.com/listing/1234567890/fear-tracker-daggerheart \
  --source instagram \
  --content fearTracker_processVid_001
```

`--medium` defaults to `organic` and `--campaign` defaults to
`phase1_reactivation`, so for typical Phase 1 use you only need `--url`,
`--source`, and `--content`. The script also accepts a positional URL form
and validates the source against the allowed list above.

## Pitfalls worth knowing

1. **Pinterest strips trailing query params on click in some flows.** If
   click-throughs show up as untagged direct/`(none)` traffic from
   `pinterest.com`, the pin landing page may be stripping UTM — open the pin
   in a clean browser and confirm the URL bar still has the params after
   click.
2. **Discord and Reddit sometimes wrap outbound links in their own redirect
   shim**, which can preserve the UTM string but show the referrer as
   `discord.com` / `out.reddit.com` instead of the platform name. The
   `utm_source` value you set is what we trust; we don't lean on
   `page_referrer`.
3. **Stories / IG bio links** are click-through containers — only the *final*
   destination URL needs UTM. Linktree-style intermediaries break attribution
   because GA4 sees the linktree page as the referrer; prefer a direct
   destination URL with UTM whenever possible.
