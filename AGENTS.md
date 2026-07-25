# CreatorFlow Agent Guidance

## Working practices

- Inspect the existing code, tests, and documentation before making changes.
- Work on a `codex/<task-name>` branch. Never push directly to `main`.
- Make focused changes for one milestone at a time and preserve existing
  functionality.
- Use the existing Python virtual environment when working locally.
- Run the complete offline test suite before reporting completion.
- Never claim tests passed unless they were actually executed, and report the
  exact results.
- Ask before installing dependencies or accessing the network.
- Stop before committing or pushing unless specifically authorized.

## Security and operational boundaries

- Never commit secrets, tokens, cookies, `.env` files, downloaded media,
  credentials, or session data.
- Never use `sudo`.
- Never modify SSH, firewall, Docker services, Docker volumes, or server
  configuration without explicit approval.
- Never deploy or publish content without explicit approval.

## Pull request workflow

- Create every completed development milestone on a feature branch based on
  the latest `origin/main`.
- Before creating a pull request, run the complete required test suite and
  `git diff --check`.
- Push the tested feature branch to `origin`.
- Check whether a pull request already exists for the feature branch before
  creating one. Never create duplicate pull requests.
- If no pull request exists, create one targeting `main` with a concise
  implementation summary, validation results, and known limitations.
- Never merge a pull request as part of initial implementation unless the user
  explicitly requests the merge.
- Before merging, inspect required checks, mergeability, conflicts, and review
  status.
- Never bypass required checks, use administrator overrides, or force-push.
- Use the repository's normal squash-merge method unless explicitly instructed
  otherwise.
- After a successful merge, update the local `main` branch, rerun the required
  test suite, and delete the merged feature branch when safe.
- Always report the pull request number or URL, commit hash, checks, merge
  result, final test results, and final Git status.

## Definition of done

- The requested behavior is implemented.
- Relevant tests are added or updated.
- The complete offline test suite is executed.
- Exact test results are reported.
- The full diff is reviewed.
- No secrets or media are included.
- Documentation is updated where necessary.
