# Self-hosted fonts

This directory carries WOFF2 files that the site loads via `@font-face`
in `../css/base.css`. Serving them locally rather than from a
third-party CDN keeps the site honest about its "no remote requests"
posture and removes a privacy side-channel.

## Inter — `InterVariable.woff2`, `InterVariable-Italic.woff2`

- Source: <https://rsms.me/inter/font-files/>
- Upstream project: <https://github.com/rsms/inter>
- Version: 4.x (variable-weight distribution)
- License: SIL Open Font License 1.1
  (<https://github.com/rsms/inter/blob/master/LICENSE.txt>)

## IBM Plex Mono — `IBMPlexMono-Regular.woff2`, `IBMPlexMono-Bold.woff2`

- Source (CDN mirror): <https://cdn.jsdelivr.net/gh/IBM/plex@master/packages/plex-mono/fonts/complete/woff2/>
- Upstream project: <https://github.com/IBM/plex>
- License: SIL Open Font License 1.1
  (<https://github.com/IBM/plex/blob/master/LICENSE.txt>)

## Updating

Re-fetch and replace the WOFF2 files in place; the CSS references them
by filename, not version. Update the version row above when you do.
Binary replacement does not require a schema or manifest version bump.
