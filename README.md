# dmut-resolvers

[![Update DNS resolvers](https://github.com/bp0lr/dmut-resolvers/actions/workflows/updateResolvers.yml/badge.svg)](https://github.com/bp0lr/dmut-resolvers/actions/workflows/updateResolvers.yml)
[![CI](https://github.com/bp0lr/dmut-resolvers/actions/workflows/ci.yml/badge.svg)](https://github.com/bp0lr/dmut-resolvers/actions/workflows/ci.yml)

Public DNS resolver lists for [dmut](https://github.com/bp0lr/dmut), generated with a pinned version of [dnsfaster](https://github.com/bp0lr/dnsfaster). Each update downloads candidates, validates DNS answers, measures reliability and latency, and publishes both lists together only after all checks pass.

> Migration note: the existing checked-in lists predate this workflow. They remain unchanged until its first successful publication. A missing `metadata.json` means that these files have no validation record from the new pipeline.

## Download

| File | Contents |
| --- | --- |
| [resolvers.txt](https://raw.githubusercontent.com/bp0lr/dmut-resolvers/main/resolvers.txt) | All passing resolvers, ordered by p95 latency, then average latency and IP address to break ties. |
| [top20.txt](https://raw.githubusercontent.com/bp0lr/dmut-resolvers/main/top20.txt) | The first up to 20 entries from the same validated list. |
| `metadata.json` | Created on the first changed-list publication: validation time, source, configuration, tool version, counts and list checksums. |

Both text files use one bare public IPv4 address per line, LF line endings, no comments and no duplicates. Their existing paths are preserved for dmut compatibility. IPv6 and nonpublic source addresses are excluded and counted in the run report; IPv6 publication is deferred until the dmut consumer supports it reliably.

Update dmut's local resolver files:

```sh
dmut --update-dnslist
```

Or download a list directly:

```sh
curl --fail --location --output resolvers.txt https://raw.githubusercontent.com/bp0lr/dmut-resolvers/main/resolvers.txt
```

## Automatic and manual updates

The **Update DNS resolvers** workflow is scheduled daily at **06:23 UTC (03:23 in Buenos Aires)**. Scheduled runs publish valid changes on the default branch. GitHub may delay scheduled jobs; in public repositories it can disable them after 60 days without repository activity. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

To preview or publish an update:

1. Open **Actions → Update DNS resolvers → Run workflow**.
2. Leave **Only validate and upload a preview** checked for the first run.
3. Read the run summary and download its `resolver-update-<run ID>-<attempt>` artifact.
4. Review `candidate/resolvers.txt`, `candidate/top20.txt`, `candidate/report.json` and the detailed results. To publish a fresh run, select the default branch and uncheck the preview option.

Runs on other branches produce previews only. Generation has read-only repository permissions. A separate publication job receives `contents: write`; it verifies the bundle again before committing only `resolvers.txt`, `top20.txt` and `metadata.json`. Branch rules must permit this bot's push. The workflow does not bypass branch protection or use a personal access token.

Concurrent updates are serialized, with active runs allowed to finish. If an unrelated documentation change reaches the branch during generation, publication rebases before pushing. If lists, scripts or pipeline configuration changed, publication stops and asks for a fresh run. Push and fetch retries are bounded; no force-push is used.

## Validation and publication rules

The source is the [public-dns.info US list](https://public-dns.info/nameserver/us.txt). Downloads use HTTPS, a size limit, time limits and at most three attempts for temporary transport errors, HTTP 429 or HTTP 5xx. Invalid content and permanent HTTP errors stop the update.

The current settings are in [update-config.json](update-config.json):

| Setting | Default |
| --- | --- |
| Go | `1.27.1` |
| dnsfaster | Exact commit `2ba7c0afdabd9fde0128a5fabf755cecf6ae2617` |
| Source limit | 1 MiB and 20,000 distinct eligible addresses |
| Measurements | 20 per resolver, 10 workers, global limit of 50 queries/second |
| Time limits | 2 seconds per query, 180 minutes for DNS work, 195 minutes for the generation job |
| Correctness | dnsfaster baseline consensus, A records, positive and negative checks, one retry for validation transport failures |
| Minimum measured success rate | 95% |
| Maximum measured p95 latency | 400 ms |
| Minimum published count | 20 |
| Maximum count decrease | 50% compared with the previous list after migration |

These are initial operational settings to review in preview runs, not guarantees about resolver quality. The source contained about 11,500 eligible addresses when this workflow was developed, so a full run can take substantial time. The query limit also covers reference and validation queries. A larger source may require reviewing the time budget. The updater does not automatically relax validation, retry the entire measurement to obtain a passing sample, or publish partial results when a deadline expires.

All input resolvers must appear exactly once in the structured result, including filtered entries. Passing results must satisfy the configured measurement and validation checks. Both lists are derived from this single dataset, with identical ranking, and their hashes are verified again before publication. A candidate older than 24 hours cannot be published by rerunning an old publication job.

The historical list has no comparable validation metadata, so the first publication applies the absolute minimum and skips the relative-drop check. Subsequent publications apply both checks. Review the first preview carefully before enabling publication.

| Outcome | Behavior |
| --- | --- |
| Validated changes | Publish both lists and their metadata in one commit, unless running a preview. |
| Validated, unchanged lists | Succeed without a commit. The run artifact records the new validation time. |
| Download, tool, validation or deadline failure | Fail with diagnostics and keep the previously published files. |
| Publication conflict or permission failure | Fail with an explanation; retain the candidate artifact for inspection. |

**Freshness:** `metadata.json` describes the validation that produced the last changed lists. An unchanged successful run does not rewrite it. Check the latest successful run's `candidate/report.json` for the latest validation time; a green CI badge only means the pipeline tests passed. Generation summaries report validation, while the publication job reports whether changes reached the branch. Artifacts are retained for 14 days.

Measurements come from a GitHub-hosted runner and can differ from your location. Agreement with reference resolvers on sampled names does not guarantee correctness for every domain, DNSSEC validation or future availability. Domains with geographically varying answers can fail consensus even when a resolver works; choose a stable validation domain you control if needed. This project does not promise a universal error rate below 1%. Use resolvers that permit your intended traffic.

## Run a local preview

Requires Python 3.10+ (standard library only) and the Go version in `update-config.json`. This repository has no Go module of its own; Go builds the pinned external dnsfaster tool.

Install the configured revision on Linux/macOS:

```sh
DNSFASTER_REF="$(python3 -c 'import json; print(json.load(open("update-config.json"))["dnsfaster_ref"])')"
go install "github.com/bp0lr/dnsfaster@$DNSFASTER_REF"
python3 scripts/update.py --output build/preview-1
```

PowerShell, using an installed Python interpreter:

```powershell
$resolverConfig = Get-Content update-config.json -Raw | ConvertFrom-Json
go install "github.com/bp0lr/dnsfaster@$($resolverConfig.dnsfaster_ref)"
python scripts/update.py --output build/preview-1
```

Put Go's binary directory on `PATH`, or pass `--dnsfaster /path/to/dnsfaster`. The output directory must be new; use another name for each preview. This prevents stale files from being mistaken for a new successful result. Local previews perform real downloads and DNS queries but never change the published lists or run Git commands. `scripts/publish.py` is restricted to the dedicated GitHub Actions job.

## Tests and maintenance

Run the offline tests without installing Go or sending DNS traffic:

```sh
python3 -m unittest discover -s tests -v
```

The tests simulate downloads, tool results and Git operations, covering malformed/empty/partial data, retry limits, timeouts, checksums, old candidates, unchanged results, count guards and publication conflicts. They do not create commits or push anything. CI also runs pinned `actionlint` to check workflow syntax and shell snippets.

Dependabot proposes weekly updates for the SHA-pinned GitHub Actions. Update `go_version` and `dnsfaster_ref` in `update-config.json` deliberately, then run the tests and an Actions preview before publishing with a new tool revision. Both workflows read the same configured Go version. Measurement changes should be reviewed together with their effect on counts and run duration.

## Troubleshooting

| Problem | What to inspect |
| --- | --- |
| No daily runs | Confirm the workflow is on the default branch and scheduled Actions are enabled. Check for inactivity suspension. |
| Tool installation fails | Read `tooling.log`; check the pinned revision, required Go version and GitHub/module-proxy availability. |
| Download fails or contains invalid text | Read `candidate/download.log` and the report. Check the source's status and format; do not substitute unchecked content. |
| No consensus or too few passing resolvers | Read `candidate/dnsfaster.log` and, when available, `candidate/results.json`. Review network availability and the validation domain before changing thresholds. |
| Time budget or source-size limit reached | Review candidate count and query budget. Incomplete runs are deliberately not published. |
| Sudden count decrease | Compare the report with previous metadata and inspect rejected results. Review a proposed threshold change explicitly. |
| No commit after a successful run | Check whether it was a preview, ran on another branch or produced unchanged lists. |
| Push rejected | Check `contents: write`, branch rules and concurrent changes. Rerun the complete workflow if the candidate is stale. |
