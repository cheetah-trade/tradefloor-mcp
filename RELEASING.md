# Releasing

A release is a tag. Everything else is automated.

## One-time setup on PyPI

Trusted Publishing, so no API token is ever stored, rotated, or leaked.

1. Sign in at [pypi.org](https://pypi.org) and open **Your projects → Publishing**
   (for a first release, use **Add a pending publisher**).
2. Fill in:
   - PyPI Project Name: `tradefloor-mcp`
   - Owner: `cheetah-trade`
   - Repository name: `tradefloor-mcp`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. In the GitHub repository, create an environment named `pypi`
   (Settings → Environments). Add a required reviewer if a release should need
   a second pair of eyes.

That is the whole setup. No secret is added to the repository.

## Cutting a release

```bash
# 1. bump the version in pyproject.toml and src/tradefloor_mcp/__init__.py
# 2. make sure they agree, and that the tests pass on the oldest supported Python
uv run --python 3.11 --isolated --no-cache --with pytest --with pytest-asyncio --with . pytest tests/ -q

# 3. tag and push
git tag v0.1.0
git push origin v0.1.0
```

The workflow runs the suite again, builds, checks the distributions, and
publishes.

## Version numbers mean something here

The three tool contracts follow semantic versioning strictly, because the README
promises it and people build on that promise.

- **Patch** — fixes that leave the three tools' inputs and outputs alone.
- **Minor** — a new tool, or a new optional field. Nothing existing moves.
- **Major** — anything that changes or removes an existing field, renames a
  tool, or alters what a field means.

Adding a term to the glossary is a patch. Renaming a key in the response is a
major, no matter how small the rename looks.

## What cannot be undone

A version published to PyPI cannot be replaced. Yanking hides it from resolvers
but the file stays downloadable forever. So the version number is a one-way
door: if `0.1.0` goes out wrong, the fix is `0.1.1`, never a re-upload.
