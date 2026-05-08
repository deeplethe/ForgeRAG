# Contributing to OpenCraig

Thank you for your interest in contributing to OpenCraig! We welcome contributions of all kinds — bug fixes, new features, documentation improvements, and more.

## Getting Started

1. **Fork** the repository and clone your fork locally
2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Install the frontend:
   ```bash
   cd web && npm install && npm run build && cd ..
   ```
4. Create a new branch for your work:
   ```bash
   git checkout -b feat/your-feature-name
   ```

## Development Workflow

### Running Tests

```bash
pytest tests/ -v
```

### Code Style

We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
ruff check .
ruff format .
```

Please ensure all checks pass before submitting a pull request.

### Running the Dev Server

```bash
python main.py --workers 1 --reload
```

## What to Contribute

- **Bug fixes** — Found a bug? Fix it and submit a PR
- **New backends** — Add support for a new vector store, database, or LLM provider
- **Documentation** — Improve docs, fix typos, add examples
- **Tests** — Increase test coverage
- **Performance** — Profile and optimize hot paths

## Pull Request Guidelines

1. Keep PRs focused — one feature or fix per PR
2. Write clear commit messages
3. Add or update tests for new functionality
4. Ensure `pytest tests/` and `ruff check .` pass
5. Update documentation if your changes affect user-facing behavior

## Reporting Issues

- Use [GitHub Issues](https://github.com/deeplethe/OpenCraig/issues) to report bugs or request features
- Include reproduction steps, expected behavior, and environment details

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
