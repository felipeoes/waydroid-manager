<!-- Base branch: dev, not main (see CONTRIBUTING.md, "Workflow"). -->

## What and why

<!-- What changes for users or developers, and why. Link the issue it fixes, if any. -->

## How it was tested

<!-- What you ran and what you checked by hand: Android versions, Graphics mode (NVIDIA, another
GPU, Software), desktop. Paste or summarize the output where it helps. -->

- [ ] Unit tests: `python3 -m unittest discover -s tests/unit -t .`
- [ ] `tests/integration/smoke.sh` passed (needed for daemon, container, network or cloning changes)

## Checklist

- [ ] One topic; commits follow [docs/code-style.md](https://github.com/felipeoes/waydroid-manager/blob/dev/docs/code-style.md)
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]`
- [ ] README (user-visible changes) or CONTRIBUTING (developer-facing ones) updated
