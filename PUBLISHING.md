# Publishing the Pi 5 OpenOCD MCP worker

Suggested separate repository: `civcode/rpi5-openocd-mcp-worker`.

## If a GitHub repository already exists (empty)

Download `rpi5-openocd-mcp-worker.git.bundle` from ChatGPT onto Oberon:

```bash
git clone ~/Downloads/rpi5-openocd-mcp-worker.git.bundle rpi5-openocd-mcp-worker
cd rpi5-openocd-mcp-worker
git remote remove origin
git remote add origin https://github.com/civcode/rpi5-openocd-mcp-worker.git
git push -u origin main
```

You can instead create an empty GitHub repo now and ask ChatGPT to publish its prepared source using the GitHub integration. Do not add a GitHub README/license initially when creating the empty repo, so that `main` is an empty remote branch.

## If the repository does not yet exist

With GitHub CLI authenticated as `civcode`, after cloning the bundle and removing its `origin`:

```bash
gh repo create civcode/rpi5-openocd-mcp-worker --public --source=. --remote=origin --push
```

Use `--private` instead of `--public` if desired. The worker includes **no GitHub Actions workflows**. Do not run workflows to validate it; run `python -m unittest discover -s tests -v` locally.

## Relationship with ATfE-port

Once the worker is published and validated on the Pi, add a documentation link in `civcode/STM32F103-BU0x_SDK` branch `ATfE-port`. A Git submodule is optional; the STM32 SDK is buildable without the Pi 5 debugging tool and the worker is deployable without the SDK repository. Avoid duplicate copies of the Python source.
