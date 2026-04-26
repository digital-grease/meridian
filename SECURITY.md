# Security policy

## Reporting a vulnerability

Send disclosures to [`dg@digitalgrease.net`](mailto:dg@digitalgrease.net).

Please include:

- A short description of the issue.
- Reproduction steps, or a proof of concept if available.
- The affected version / commit SHA (visible in every site page footer).
- Any disclosure-timeline constraints you have.

We'll acknowledge within 72 hours and aim to fix verified reports within
30 days. For anything likely to be exploited in the wild, faster.

## What counts as a security issue

- A way for an external actor to inject content into the public site
  that isn't in the source git history.
- A way to cause the site build, pipeline run, or link-rot guard to
  report success while silently producing broken output.
- Credential exposure (API keys, AWS keys, etc.) in any committed file.
- A way to make the held-out corpus set reachable by a provider.
- Any privilege escalation in the pipeline's storage layer (e.g. a
  way to cause one run to overwrite another's raw samples).

## What does *not* count as a security issue

- Accuracy of the drift measurements themselves — file those as bugs
  against methodology.
- Claims about specific provider model behaviors — that's the whole
  point of the project; bring data.
- Missing rate limits on the pipeline side — the pipeline is run by
  maintainers, not exposed as a public endpoint.

## Safe harbor

Good-faith security research is welcome and will not be met with legal
action. "Good faith" excludes: active attacks on the production
deployment, social engineering of contributors, attempts to exfiltrate
the held-out corpus.
