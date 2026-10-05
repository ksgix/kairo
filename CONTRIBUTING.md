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

To run a single module, keep using discover:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_deploy.py'
```

Some test modules import helpers from sibling test modules, so the dotted form
(`python3 -m unittest tests.test_deploy`) fails at import time. The tests need no network,
credentials or third-party packages. If a change needs one of those, say so in the pull request.

## Making changes

- Keep changes focused. One concern per commit or pull request.
- Add or update tests with behaviour changes. Where you can, reproduce a defect with a failing
  test first.
- Update the docs in [docs/](docs/README.md) when behaviour or the operator interface changes.
- Write commit messages as a short imperative summary line (for example "Add CI workflow
  running the unittest suite"), with more detail in the body if it helps.

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
