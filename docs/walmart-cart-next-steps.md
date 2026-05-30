# Walmart Cart — Next Steps

## Problem

Playwright's bundled Chromium triggers Walmart's bot detection ("Robot or human?" CAPTCHA). `playwright-stealth` + `channel="chrome"` did not resolve it.

## Solution: CDP to real Chrome

Connect to the user's actual running Chrome instance via Chrome DevTools Protocol instead of launching a separate browser. Walmart sees the real browser with real cookies, real fingerprint, existing session — completely undetectable.

## Implementation steps

### 1. Shell alias for Chrome with remote debugging

Add to `~/.zshrc`:

```bash
alias chrome-debug='/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --profile-directory=Default &'
```

Run `chrome-debug` once before running `python main.py cart`. Chrome opens normally; you stay logged into Walmart as usual.

### 2. Update `cart_filler.py`

Replace `launch_persistent_context` with `connect_over_cdp`:

```python
async def fill(self) -> CartResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("pip install playwright && playwright install chromium")

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception:
            raise RuntimeError(
                f"Could not connect to Chrome at {self.cdp_url}.\n"
                "Start Chrome with: chrome-debug  (see docs/walmart-cart-next-steps.md)"
            )
        context = browser.contexts[0]  # use the existing context (already logged in)
        page = await context.new_page()
        try:
            return await self._run_agent_loop(page)
        finally:
            await page.close()
```

The `_ensure_session` check becomes optional since the existing Chrome session is already logged in, but worth keeping as a fast sanity check.

### 3. Update `CartFiller.__init__`

Replace `profile_dir` / `headless` with `cdp_url`:

```python
self.cdp_url = config.get("cdp_url", "http://localhost:9222")
```

### 4. Update `config.yaml` / `config.yaml.template`

```yaml
walmart:
  enabled: true
  cdp_url: "http://localhost:9222"
```

Remove `profile_dir` and `headless` — they're no longer used.

### 5. Update `main.py` `cmd_cart`

Remove the "Launching browser" print and replace with:

```
Connecting to Chrome at http://localhost:9222 ...
(Start Chrome with debug port if this fails: chrome-debug)
```

### 6. Update README

Add a "First-time setup" note under Walmart:
- Add the `chrome-debug` alias to `~/.zshrc`
- Log into Walmart in that Chrome window
- Future runs: just have Chrome open (alias starts it if not already running)

### 7. Tests

`TestCartFillerAgentLoop` mocks `filler.client.messages.create` — the Playwright layer is already mocked via `_mock_page()`. The CDP connection path just needs the `connect_over_cdp` call mocked similarly to how `launch_persistent_context` was mocked. No structural test changes needed.

## Files to change

- `cart_filler.py` — swap `launch_persistent_context` → `connect_over_cdp`
- `config.yaml` — swap `profile_dir`/`headless` → `cdp_url`
- `config.yaml.template` — same
- `main.py` — update connection message, remove headless/profile_dir references in `cmd_cart`
- `README.md` — update setup instructions
