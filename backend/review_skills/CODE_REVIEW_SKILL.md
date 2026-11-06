# Code Review Skill integration

Preflight's `Preflight Code Review` button uses a progressive, technology-aware
review protocol derived from [awesome-skills/code-review-skill](https://github.com/awesome-skills/code-review-skill), licensed under MIT.

At review time, Preflight identifies the imported repository's language and framework
from its source and manifests, selects the relevant guides, and makes them mandatory
instructions for the configured review model. Universal security, code-quality, and
performance guides are always included. No source code is installed, built, or run.

The executable selector is `app/core/review_skill_selector.py`.
