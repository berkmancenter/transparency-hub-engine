# Contributing to Transparency Archiver

Thank you for your interest in contributing to the Transparency Archiver! We welcome contributions from the community.

## About This Project

The Transparency Archiver is a Python-based document capture pipeline that powers the [Transparency Hub](https://hub.transparency.berkmancenter.org), a project of the [Berkman Klein Center for Internet & Society](https://cyber.harvard.edu/) at Harvard University.

This repo is part of a three-repo ecosystem — see [Related Repositories](#related-repositories) below.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How to Contribute](#how-to-contribute)
- [Development Setup](#development-setup)
- [Coding Guidelines](#coding-guidelines)
- [Submitting Changes](#submitting-changes)
- [Reporting Bugs](#reporting-bugs)
- [Feature Requests](#feature-requests)
- [Related Repositories](#related-repositories)

## Code of Conduct

Please read our [Code of Conduct](CODE_OF_CONDUCT.md). We are committed to providing a welcoming and inclusive environment for all contributors.

## How to Contribute

There are many ways to contribute:

- **Report bugs** and issues
- **Suggest new features** or enhancements
- **Improve documentation**
- **Submit bug fixes** or new features
- **Improve test coverage**

## Development Setup

### Prerequisites

- Python 3.13.3+
- Docker (for running the Browsertrix Crawler fork)
- Git
- MongoDB (local or cloud instance)

### Getting Started

1. **Fork the repository** on GitHub

2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/your-username/transparency-hub-engine.git
   cd transparency__archives_dev
   ```

3. **Add upstream remote**:
   ```bash
   git remote add upstream https://github.com/berkmancenter/transparency-hub-engine.git
   ```

4. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

5. **Install dev dependencies and pre-commit hooks**:
   ```bash
   pip install -r requirements-dev.txt
   pre-commit install
   ```

6. **Set up environment variables** (see [README.md](README.md) for details):
   ```bash
   export MONGODB_URL='mongodb://localhost:27017/'
   export GCP_BUCKET='your-bucket-name'
   ```

7. **Build the Browsertrix Crawler fork** — this pipeline requires a local Docker image from the [Browsertrix Crawler fork](https://github.com/berkmancenter/transparency-hub-browsertrix-crawler). See that repo for build instructions.

## Coding Guidelines

### Code Style

- This project uses **`ruff`** for formatting and linting
- Pre-commit hooks run `ruff format` and `ruff check --fix` automatically on every commit
- Run manually: `ruff format && ruff check --fix`

### Commit Messages

- Use the imperative mood ("Add feature" not "Added feature")
- Keep the first line under 72 characters
- Reference issue numbers when applicable

## Submitting Changes

### Before Submitting

1. **Sync with upstream**:
   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

2. **Run linting**:
   ```bash
   ruff format && ruff check --fix
   ```

3. **Test your changes** with a dry run:
   ```bash
   python main.py --dry_run true
   ```

### Pull Request Process

1. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** and commit them with clear messages

3. **Push to your fork**:
   ```bash
   git push origin feature/your-feature-name
   ```

4. **Open a Pull Request** on GitHub:
   - Provide a clear title and description
   - Reference any related issues
   - Describe how to test your changes

5. **Respond to feedback** from reviewers

### Pull Request Guidelines

- Keep PRs focused on a single feature or fix
- Update documentation if needed
- Ensure the PR description explains the "why" not just the "what"

## Reporting Bugs

If you find a bug, please open an issue with:

- **Clear title** describing the problem
- **Steps to reproduce** the bug
- **Expected behavior** vs actual behavior
- **Environment details** (OS, Python version, Docker version)
- **Error messages** or log output

## Feature Requests

We welcome feature suggestions! When requesting a feature:

- **Check existing issues** first to avoid duplicates
- **Describe the use case** — why is this feature needed?
- **Provide examples** of how it would work
- **Consider the scope** — is it aligned with the project's goals?

## Questions?

If you have questions about contributing:

- Check the [README](README.md)
- Open a GitHub Discussion
- Review existing issues and PRs

## Related Repositories

| Repository | Description |
|-----------|-------------|
| **[Transparency Hub](https://github.com/berkmancenter/transparency-hub)** | Next.js frontend — the public-facing website |
| **[Transparency Archiver](https://github.com/berkmancenter/transparency-hub-engine)** (this repo) | Python pipeline that crawls and archives policy documents |
| **[Browsertrix Crawler Fork](https://github.com/berkmancenter/transparency-hub-browsertrix-crawler)** | Custom fork of Browsertrix Crawler used by this pipeline |

## License

By contributing to Transparency Archiver, you agree that your contributions will be licensed under the [AGPL-3.0 License](LICENSE).

---

Thank you for contributing to Transparency Archiver!
