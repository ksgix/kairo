# Contributing to Kairo

Thanks for your interest. Kairo is a small, dependency-free Python project, and contributing
should take very little setup.

## Development setup

- Python 3.12 on Linux.
- No third-party packages are required to run Kairo or its tests.

```sh
git clone https://github.com/ksgix/kairo.git
cd kairo
export PYTHONPATH=src
python3 -m kairo --help
```

Optionally, `pip install -e .` installs the `kairo` command.

## Running the tests

Run the suite the way CI does ([.github/workflows/tests.yml](.github/workflows/tests.yml)):

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

To run a single module, use discover with a pattern or the dotted module name:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_deploy.py'
PYTHONPATH=src python3 -m unittest tests.test_deploy
```

Test modules share helpers by importing sibling modules; `tests/__init__.py` puts
`tests/` on `sys.path` so both forms work. The tests need no network,
credentials or third-party packages. If a change needs one of those, say so in the pull request.

## Making changes

- Keep changes focused. One concern per commit or pull request.
- Add or update tests with behaviour changes. Where you can, reproduce a defect with a failing
  test first.
- Update the docs in [docs/](docs/README.md) when behaviour or the operator interface changes.
- Write commit messages as a short imperative summary line (for example "Add CI workflow
  running the unittest suite"), with more detail in the body if it helps.

## Developing with Claude Code

`scripts/setup-claude-code.sh` writes this checkout's Claude Code permissions to `.claude/settings.local.json`. The file is machine-specific and ignored by git. Run the script as the account that runs Claude Code, then restart Claude Code from the project root. It preserves unrelated settings, refuses existing rules that would defeat the model (such as `Bash(*)` or a `sudo` wildcard), and `--dry-run` shows the changes without writing.

- **Mode:** `dontAsk`. Anything not allowed is denied without waiting for a person.
- **Allowed:**
  - reading and editing project files;
  - the read-only commands Claude Code vets itself (`git status`/`diff`/`log`, `ls`, …);
  - `git add`, `commit`, `rm`, `mv`, `switch -c` and `tag -a`;
  - `git fetch origin`, `git pull --ff-only origin main` and `git push origin main`;
  - `scripts/test.sh`;
  - `status`, `is-active` and `restart` of `kairo` and `kairo-dashboard`.
- **Denied:**
  - edits to `.claude/`, `.git/`, the bootstrap script and `~/.claude` (the policy cannot rewrite itself);
  - the credential files;
  - any other `sudo` or `systemctl` action;
  - interpreters and package managers with free arguments;
  - any other git network operation.
- **Not a sandbox:** the tests run project code, which Claude Code can edit, as the same account, so editing plus testing amounts to running any code that account can run. The rules bound what Claude Code does directly; isolation from the host needs an OS boundary, such as Claude Code's sandbox (which needs `bubblewrap` and `socat`) or a separate account without sudo.

## Kairo maintains itself

Kairo can edit, test and commit its own code in a development repository, and it deploys a
committed revision through its own deploy path. Some commits in this repository were written
that way. Two things follow from this:

- Committed code is not running code. A revision runs only after it is deployed as an immutable
  release, which passes preflight (the full test suite and a dry cycle) and is then confirmed by
  the restarted runtime. See [Self-maintenance](docs/self-maintenance.md).
- Changes to the deploy, supervisor and verification code are trust-critical. Review them with
  extra care, and include tests that exercise the failure and rollback paths.

## Reporting security issues

Please do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
