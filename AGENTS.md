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

## Definition of done

- The requested behavior is implemented.
- Relevant tests are added or updated.
- The complete offline test suite is executed.
- Exact test results are reported.
- The full diff is reviewed.
- No secrets or media are included.
- Documentation is updated where necessary.
