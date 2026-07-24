# Watched-source discovery canary

The `watched_source_discovery` service parses an already-fetched RSS/Atom feed
or sitemap into explicit, bounded enrollment candidates. It never fetches a
URL, enrolls a candidate, changes a refresh policy, or subscribes to WebSub.

For the k3s-lab canary, an operator must supply a per-run allowlist and a small
candidate cap. Candidates outside that host allowlist (and duplicates) are
recorded as rejected. Advertised WebSub hubs are evidence only; callbacks stay
disabled until a separate, reviewed subscription design exists.

Source-class defaults are deliberately conservative: feeds target 30 minutes,
webpages 24 hours, and sitemaps 12 hours. Any later dispatcher integration
must persist provenance and policy before setting a resource active. The canary
remains disabled until the Helm configuration and a compliance report can prove
that at least 95% of enrolled resources were checked within their policy window.
The schema keeps `webpage` as its database default so existing direct writers
and migration probes remain compatible while new discovery paths record a more
specific class explicitly.

Scheduled refresh dispatch has two independent gates: the explicit enable flag
and an exact-host allowlist. An empty allowlist dispatches nothing, wildcards,
URLs, credentials, and ports are rejected, and both the database query and the
worker re-check the resource hostname before taking a lease. The initial canary
must use a dedicated internal service hostname and a batch size of one.

Rollback: set dispatch disabled (the default) or clear the host allowlist.
Existing source records and prior successful versions remain untouched; rollback
does not delete resources, source records, items, or source content.

After the internal fixture and allowlist are deployed, preview the single
dedicated row with:

```bash
python scripts/seed_watched_source_canary.py
```

Only an explicitly approved run may add it:

```bash
python scripts/seed_watched_source_canary.py --write
```

The command is locked to tenant `sar-1207-canary` and the internal service
hostname. It is idempotent and never updates or deletes an existing row.
