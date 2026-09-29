"""
cart_filler.py

Fills a Walmart cart from a GroceryList using an iterative Claude agent loop
backed by Playwright browser automation.

Connects to the user's running Chrome instance via CDP (Chrome DevTools Protocol)
so Walmart sees a real browser with a real session — no bot detection.

One-time setup:
  1. Add to ~/.zshrc:
       alias chrome-debug='/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --profile-directory=Default &'
  2. Run: chrome-debug
  3. Log into Walmart in that Chrome window.
  Future runs: just have Chrome open (the alias starts it if not already running).

Requires: pip install playwright
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import anthropic

from grocery_builder import GroceryList

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pantry staples — never add these to the cart; assumed always on hand
# ---------------------------------------------------------------------------

_PANTRY_STAPLE_RE = re.compile(
    r"\b("
    r"salt|pepper|black pepper|white pepper|paprika|smoked paprika|"
    r"cumin|coriander|turmeric|cinnamon|oregano|thyme|rosemary|basil|"
    r"bay leaf|bay leaves|cayenne|chili flakes|red pepper flakes|"
    r"garlic powder|onion powder|garam masala|curry powder|allspice|"
    r"nutmeg|cloves|cardamom|sumac|za.atar|chili powder|"
    r"Italian seasoning|herbes de Provence|Chinese five.spice|"
    r"seasoned salt|kosher salt|sea salt|table salt"
    r")\b",
    re.IGNORECASE,
)


def _is_pantry_staple(name: str) -> bool:
    return bool(_PANTRY_STAPLE_RE.search(name))


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------

@dataclass
class CartItem:
    name: str
    status: str                   # "added" | "skipped" | "needs_review" | "not_found"
    walmart_name: Optional[str] = None
    note: Optional[str] = None


@dataclass
class CartResult:
    added:          list[CartItem] = field(default_factory=list)
    needs_review:   list[CartItem] = field(default_factory=list)
    skipped:        list[CartItem] = field(default_factory=list)
    not_found:      list[CartItem] = field(default_factory=list)
    pantry_staples: list[str]      = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return len(self.added) + len(self.needs_review) + len(self.skipped) + len(self.not_found)


# ---------------------------------------------------------------------------
# Tool definitions — Claude calls these; Python executes them via Playwright
# ---------------------------------------------------------------------------

WALMART_TOOLS = [
    {
        "name": "search_walmart",
        "description": (
            "Search Walmart for a grocery item. Returns up to 6 numbered results "
            "with product name, price, and size. Use the result index with add_to_cart."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":    {"type": "string", "description": "Item to search for, e.g. 'ground beef 80/20'"},
                "quantity": {"type": "string", "description": "Desired quantity, e.g. '1.5 lb' — for reference when choosing"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "add_to_cart",
        "description": (
            "Add a product to the cart by its index number from the most recent search_walmart call. "
            "Returns confirmation or an error message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "result_index": {
                    "type": "integer",
                    "description": "0-based index from the last search_walmart results",
                },
            },
            "required": ["result_index"],
        },
    },
    {
        "name": "finish_cart",
        "description": (
            "Call this exactly once after processing every item on the grocery list. "
            "Do not call until all items have been handled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "added": {
                    "type": "array",
                    "description": "Items successfully added to the cart.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":         {"type": "string"},
                            "walmart_name": {"type": "string"},
                            "status":       {"type": "string", "enum": ["added"]},
                        },
                        "required": ["name", "walmart_name", "status"],
                    },
                },
                "needs_review": {
                    "type": "array",
                    "description": "Items with too many ambiguous matches to choose confidently.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":   {"type": "string"},
                            "note":   {"type": "string"},
                            "status": {"type": "string", "enum": ["needs_review"]},
                        },
                        "required": ["name", "note", "status"],
                    },
                },
                "skipped": {
                    "type": "array",
                    "description": "Items already in the cart — not added again.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":   {"type": "string"},
                            "status": {"type": "string", "enum": ["skipped"]},
                        },
                        "required": ["name", "status"],
                    },
                },
                "not_found": {
                    "type": "array",
                    "description": "Items with no close match found on Walmart.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name":   {"type": "string"},
                            "note":   {"type": "string"},
                            "status": {"type": "string", "enum": ["not_found"]},
                        },
                        "required": ["name", "status"],
                    },
                },
            },
            "required": ["added", "needs_review", "skipped", "not_found"],
        },
    },
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

CART_SYSTEM_PROMPT = """You are filling a Walmart grocery cart on behalf of a household.
You have two browser tools: search_walmart and add_to_cart.

## RULES

- NEVER proceed to checkout. Fill the cart only.
- Work through the grocery list item by item.
- For each item:
  1. Call search_walmart with the item name (and quantity for reference).
  2. Pick the best match from the results:
     - Prefer any product the household has bought before (look for "Buy Again" label in the name).
     - Otherwise prefer store brand (Great Value) when available.
     - Match the requested quantity as closely as possible.
  3. If the item is already in the cart (result shows "In cart"), mark it skipped.
  4. If there are 3+ plausible options with no clear winner, mark it needs_review.
  5. If nothing relevant is returned, mark it not_found.
  6. Otherwise call add_to_cart with the chosen result_index.
- Do not ask for confirmation per item — process the entire list, then call finish_cart.
- finish_cart must list every item: added, needs_review, skipped, or not_found."""

# ---------------------------------------------------------------------------
# CartFiller
# ---------------------------------------------------------------------------

class CartFiller:
    def __init__(self, config: dict, grocery: GroceryList):
        self.client      = anthropic.Anthropic(api_key=config.get("api_key") or None)
        self.model       = config.get("model", "claude-opus-4-5")
        self.cdp_url     = config.get("cdp_url", "http://localhost:9222")
        self.grocery     = grocery
        self.max_iterations = 150   # ~3 tool calls per item × 50 items
        # How long to wait for the user to solve Walmart's "press & hold"
        # human check in the Chrome window before giving up on the run.
        self.human_check_timeout_s = int(config.get("walmart_human_check_timeout_s", 180))
        self._blocked = False
        self._last_results: list[dict] = []   # most recent search_walmart results

    async def fill(self) -> CartResult:
        """Connect to running Chrome via CDP and run the cart-filling agent loop."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is required for cart filling.\n"
                "Install it with: pip install playwright && playwright install chromium"
            )

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.connect_over_cdp(self.cdp_url)
            except Exception:
                raise RuntimeError(
                    f"Could not connect to Chrome at {self.cdp_url}.\n"
                    "Start Chrome with remote debugging enabled:\n"
                    "  chrome-debug\n"
                    "(Add the alias to ~/.zshrc — see README for setup.)"
                )
            if not browser.contexts:
                raise RuntimeError(
                    "Chrome is running but has no open windows. Open a tab and try again."
                )
            context = browser.contexts[0]
            page = await context.new_page()
            try:
                await self._ensure_session(page)
                return await self._run_agent_loop(page)
            finally:
                await page.close()

    # -----------------------------------------------------------------------
    # Session management
    # -----------------------------------------------------------------------

    async def _ensure_session(self, page) -> None:
        """Verify Walmart session is active in the connected Chrome instance."""
        await page.goto("https://www.walmart.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        sign_in = await page.query_selector('a[link-identifier="header-signin"], [data-testid="header-signin-btn"]')
        if sign_in is None:
            log.debug("Walmart session active.")
            return

        raise RuntimeError(
            "Not logged into Walmart in the connected Chrome window.\n"
            "Please log into Walmart in Chrome, then run the cart command again."
        )

    # -----------------------------------------------------------------------
    # Agent loop
    # -----------------------------------------------------------------------

    async def _run_agent_loop(self, page) -> CartResult:
        buy_items, pantry_staples = self._split_items()
        if not buy_items:
            log.info("No items to buy after filtering pantry staples.")
            return CartResult(pantry_staples=pantry_staples)

        messages = [{"role": "user", "content": self._build_user_prompt(buy_items)}]

        for iteration in range(self.max_iterations):
            response = self.client.messages.create(
                model=self.model,
                # Live-verified 2026-09-18: claude-sonnet-5 runs adaptive
                # thinking by default, and thinking tokens count against
                # max_tokens. 4096 was fine for the old model (no default
                # thinking) but now truncates mid-thought before any
                # tool_use block is emitted (stop_reason="max_tokens",
                # empty content) — the agent silently stalls forever.
                max_tokens=16000,
                system=CART_SYSTEM_PROMPT,
                tools=WALMART_TOOLS,
                tool_choice={"type": "auto"},
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_calls = [b for b in response.content if b.type == "tool_use"]
            if not tool_calls:
                text = next((b.text for b in response.content if b.type == "text"), "")
                log.warning(
                    "Agent produced no tool calls at iteration %d (stop_reason=%s): %s",
                    iteration, response.stop_reason, text[:1000],
                )
                break

            tool_results = []
            cart_result = None

            for tc in tool_calls:
                if tc.name == "finish_cart":
                    cart_result = self._parse_finish_cart(tc.input, pantry_staples)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": "Cart fill complete.",
                    })
                else:
                    content = await self._execute_tool(page, tc)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": content,
                    })

            messages.append({"role": "user", "content": tool_results})

            if cart_result is not None:
                return cart_result

        log.error("Cart filler hit max iterations (%d) without calling finish_cart.", self.max_iterations)
        return CartResult(pantry_staples=pantry_staples)

    # -----------------------------------------------------------------------
    # Tool dispatch
    # -----------------------------------------------------------------------

    async def _execute_tool(self, page, tool_call) -> str:
        if tool_call.name == "search_walmart":
            result = await self._search_walmart(page, **tool_call.input)
        elif tool_call.name == "add_to_cart":
            result = await self._add_to_cart(page, **tool_call.input)
        else:
            result = f"Unknown tool: {tool_call.name}"
        # Without this, a live run's log shows only API round-trips — no way
        # to tell which product the agent picked or what the page returned.
        log.info("tool %s %s -> %s", tool_call.name, tool_call.input, result)
        return result

    # -----------------------------------------------------------------------
    # Playwright tool implementations
    # -----------------------------------------------------------------------

    async def _search_walmart(self, page, query: str, quantity: str = "") -> str:
        if self._blocked:
            return self._blocked_message()

        url = f"https://www.walmart.com/search?q={quote(query)}"
        await page.goto(url, wait_until="domcontentloaded")

        # Live-verified 2026-09-28: results normally render <1s after
        # domcontentloaded, so wait for tiles rather than a fixed sleep. When
        # none appear it's usually Walmart's "press & hold" robot check, not
        # an empty search — the old fallback (inner_text("main"), 30s default
        # timeout, no <main> on that page) turned each one into a 30s stall
        # reported as "No results", and the agent kept re-searching into it.
        if not await self._wait_for_tiles(page, 10_000):
            if not await self._is_human_check(page):
                return f"No products found for '{query}'."
            print(
                "\n⚠️  Walmart is showing a 'press & hold' human check in the Chrome window.\n"
                f"   Please press and hold the button there — waiting up to "
                f"{self.human_check_timeout_s // 60} min...\n", flush=True,
            )
            log.warning("Walmart human check on search '%s' — waiting for user.", query)
            if not await self._wait_for_tiles(page, self.human_check_timeout_s * 1000):
                self._blocked = True
                log.error("Human check not completed within %ds — stopping searches.",
                          self.human_check_timeout_s)
                return self._blocked_message()
            log.info("Human check cleared — continuing.")

        results = await page.evaluate(r"""() => {
            const out = [];
            const tiles = document.querySelectorAll('[data-item-id]');
            tiles.forEach((tile, i) => {
                if (i >= 6) return;
                // Live-verified 2026-09-28: querySelector with a selector
                // list returns the first match in document order, which was
                // the link — its text carries "Bought 5+ times, " / "Rollback, "
                // prefixes. product-title holds the clean name. itemprop=price
                // no longer exists; the price is in product-price text
                // ("current price $4.48") or a "Price $ 3.30 ..." aria-label.
                const nameEl  = tile.querySelector('[data-automation-id="product-title"]')
                             || tile.querySelector('a[link-identifier]');
                const priceEl = tile.querySelector('[data-automation-id="product-price"], [aria-label^="Price"]');
                const sizeEl  = tile.querySelector('[data-automation-id="product-description"]');
                const inCart  = tile.querySelector('[data-automation-id="fulfillment-badge"]');
                const name    = nameEl  ? nameEl.textContent.trim()            : null;
                let price = '?';
                if (priceEl) {
                    const src = (priceEl.getAttribute('aria-label') || '') + ' ' + priceEl.textContent;
                    const m = src.match(/current price (?:Now )?\$\s?([\d,]+\.\d{2})/i)
                           || src.match(/Price \$\s?([\d,]+\.\d{2})/);
                    if (m) price = '$' + m[1];
                }
                const size    = sizeEl  ? sizeEl.textContent.trim()            : '';
                const badge   = inCart  ? inCart.textContent.trim()            : '';
                const item_id = tile.getAttribute('data-item-id');
                if (name) out.push({ index: i, item_id, name, price, size, badge });
            });
            return out;
        }""")

        self._last_results = results

        if not results:
            return f"No products found for '{query}'."

        lines = [f"Results for '{query}'" + (f" (want: {quantity})" if quantity else "") + ":"]
        for r in results:
            parts = [f"  {r['index']}. {r['name']}"]
            if r["size"]:
                parts.append(f"({r['size']})")
            parts.append(f"— {r['price']}")
            if r["badge"]:
                parts.append(f"[{r['badge']}]")
            lines.append(" ".join(parts))
        return "\n".join(lines)

    async def _add_to_cart(self, page, result_index: int) -> str:
        # Live-verified 2026-09-28: the displayed index is the DOM tile index
        # and can have gaps (title-less tiles are skipped), so it must be
        # resolved against r["index"], not list position — otherwise the
        # tool clicked one product and reported another, and the agent
        # "corrected" by adding more (4 milks for 2 list lines). Click the
        # tile by its item id too, since lazy-loaded carousels can shift
        # DOM positions between search and add.
        chosen = next((r for r in (self._last_results or []) if r["index"] == result_index), None)
        if chosen is None:
            return f"Error: no result at index {result_index}. Run search_walmart first."

        if chosen.get("item_id"):
            tile = await page.query_selector(f'[data-item-id="{chosen["item_id"]}"]')
        else:
            tiles = await page.query_selector_all('[data-item-id]')
            tile = tiles[result_index] if result_index < len(tiles) else None

        if tile is None:
            return f"Could not locate tile for index {result_index} — page may have changed."
        # Walmart's automation-id and button text have both changed over time
        # (confirmed live 2026-09-18: current markup is
        # data-automation-id="add-to-cart" with visible text just "Add", not
        # "add-to-cart-btn" / "Add to cart"). Try current markup first, keep
        # the older selectors as fallback in case Walmart A/B tests variants,
        # plus the aria-label which has stayed stable ("Add - <product name>").
        btn  = await tile.query_selector(
            '[data-automation-id="add-to-cart"], '
            '[data-automation-id="add-to-cart-btn"], '
            'button[aria-label^="Add - "], '
            'button:has-text("Add to cart"), button:has-text("Add to Cart")'
        )

        if btn is None:
            # Live-verified 2026-09-28 (cart page): an in-cart item shows a
            # quantity stepper — data-testid="quantity-stepper-inc-button",
            # aria-label "Increase quantity <name>, Current Quantity N" — not
            # an "In cart" button. Old selectors kept as fallback.
            in_cart = await tile.query_selector(
                '[data-testid="quantity-stepper-inc-button"], '
                'button[aria-label^="Increase quantity"], '
                'button:has-text("In cart"), [data-automation-id="in-cart-btn"]'
            )
            if in_cart:
                label = await in_cart.get_attribute("aria-label")
                m = re.search(r"Current Quantity (\d+)", label) if isinstance(label, str) else None
                qty = f" (qty {m.group(1)})" if m else ""
                return f"Already in cart{qty}: {chosen['name']}"
            return f"No 'Add to cart' button found for: {chosen['name']}"

        # force=True: live-verified 2026-09-18 that a plain click times out
        # here — Playwright's default click hovers the element first, which
        # triggers a Walmart quick-view overlay that then intercepts the
        # click itself. The button is genuinely the right target; force
        # skips the hover/visibility dance and clicks it directly.
        await btn.click(force=True)
        await page.wait_for_timeout(1500)
        return f"Added to cart: {chosen['name']}"

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _wait_for_tiles(self, page, timeout_ms: int) -> bool:
        try:
            await page.wait_for_selector("[data-item-id]", timeout=timeout_ms)
            return True
        except Exception:
            return False

    async def _is_human_check(self, page) -> bool:
        # Walmart's bot wall (title "Robot or human?", "Press & Hold"
        # button, sometimes a /blocked URL). Live-verified 2026-09-28 that
        # this fires on the real wall and not on a normal results page.
        try:
            title = await page.title()
            text  = await page.inner_text("body", timeout=3000)
        except Exception:
            return False
        haystack = f"{page.url} {title} {text[:2000]}".lower()
        return any(k in haystack for k in (
            "robot or human", "press & hold", "press and hold", "/blocked", "confirm you are human",
        ))

    def _blocked_message(self) -> str:
        return (
            "BLOCKED: Walmart's human-verification check was not completed, so "
            "searching is unavailable for the rest of this run. Do not retry. "
            "Call finish_cart now: report items already added as added, and every "
            "remaining item as not_found with note 'blocked by Walmart human check'."
        )

    def _split_items(self) -> tuple[list, list[str]]:
        buy, staples = [], []
        for item in self.grocery.all_items:
            if _is_pantry_staple(item.name):
                staples.append(item.name)
            else:
                buy.append(item)
        return buy, staples

    def _build_user_prompt(self, items: list) -> str:
        lines = [
            f"Fill the Walmart cart with the following {len(items)} grocery items.",
            "",
            "## GROCERY LIST",
        ]
        for item in items:
            qty = f"{item.quantity} {item.unit}".strip() if item.quantity else ""
            qty_str = f" — {qty}" if qty else ""
            lines.append(f"  - {item.name}{qty_str}  [{item.category}]")
        lines += ["", "Process every item, then call finish_cart() with the results."]
        return "\n".join(lines)

    def _parse_finish_cart(self, tool_input: dict, pantry_staples: list[str]) -> CartResult:
        # Live-verified 2026-09-28: the API doesn't enforce the schema's
        # "required" list, and claude-sonnet-5 omitted status → TypeError
        # after every item was already in the cart. The bucket key *is* the
        # status, so derive it; keep only known fields so stray keys can't
        # crash it either.
        def _items(key: str) -> list[CartItem]:
            return [
                CartItem(
                    name=i.get("name", ""),
                    status=key,
                    walmart_name=i.get("walmart_name"),
                    note=i.get("note"),
                )
                for i in tool_input.get(key, [])
            ]
        return CartResult(
            added=_items("added"),
            needs_review=_items("needs_review"),
            skipped=_items("skipped"),
            not_found=_items("not_found"),
            pantry_staples=pantry_staples,
        )
