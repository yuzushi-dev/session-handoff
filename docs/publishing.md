# Publishing session-handoff

Publishing runs only from the manually dispatched .github/workflows/publish.yml
workflow. The workflow checks out a v-prefixed tag, requires its commit to be
on main, requires a matching package.json version and an existing, non-draft
GitHub release, then runs npm pack --dry-run before publishing.

The workflow defaults to a dry run and the latest dist-tag. Set dry-run to
false only for an intentional release, and choose the npm dist-tag explicitly
for prereleases such as jev. It uses Node 24 and npm 11.15 or newer. npm
Trusted Publishing supplies the short-lived OIDC credential; the workflow
contains no npm token.

Configure the npm trusted publisher for package session-handoff, repository
yuzushi-dev/session-handoff, workflow filename publish.yml, and no environment.

The package metadata must contain this exact repository URL:
https://github.com/yuzushi-dev/session-handoff.git. Older release tags may
predate that metadata; their dry runs warn and remain validation-only. Future
published tags must include it.

To configure the npm side (once, with npm account proof of presence), run:

    npm trust github session-handoff --file publish.yml --repo yuzushi-dev/session-handoff --allow-publish --yes

To exercise validation from the main branch:

    gh workflow run publish.yml --ref main -f tag=v0.7.4-jev.3 -f dry-run=true -f dist-tag=latest

The dry run validates and packs the release but does not exercise the OIDC
publish exchange. The first real publish requires the npm trusted-publisher
configuration and its account verification.
