<!-- One topic per PR. Title in Conventional Commits form: feat(scope): ..., fix(scope): ..., docs: ... -->

## What

<!-- One or two sentences: what changes and why. Link the issue if there is one: Closes #123 -->

## How

<!-- Anything a reviewer needs to know: design choices, alternatives rejected, follow-ups left out. -->

## Checklist

- [ ] `docker compose -f docker-compose.test.yml run --rm --build tests` passes
- [ ] New behaviour and bug fixes come with tests
- [ ] `docs/` and `CHANGELOG.md` are updated if behaviour changed
- [ ] No secret, mailbox, real hostname or PST content in the diff
- [ ] Commits are signed off (`git commit -s`)
