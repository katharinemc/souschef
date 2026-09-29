# Resuming This Project

**Session ended:** 2026-09-28
**Status:** Walmart cart fill works end to end. It completed a full live run for
week `2026-09-21`, and every item the log reported as added was the product actually in
the cart. Four cart_filler bugs fixed today, all live-verified. One open item
(below) needs a real add-to-cart to verify, and a few cleanup chores remain.

The previous handoff (2026-09-18) is in git history (`git show f2858f8:docs/RESUME.md`).

---

## Start here next session

1. **Chrome debug profile.** Check `curl -s http://localhost:9222/json/version`.
   If it's not running:
   ```bash
   nohup "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --remote-debugging-port=9222 \
     --user-data-dir="$HOME/.chrome-debug-profile" \
     --profile-directory=Default > /dev/null 2>&1 &
   ```
   Tip: when pasting a `!` command into Claude Code, keep it on one line with
   no leading spaces. A wrapped paste gets sent as a chat message instead.
2. **Cart state at end of session:** the user's 8 own items, plus 6 test
   items from the 2026-09-21 run (Italian parsley, GV whole milk gallon, GV
   heavy cream 16 oz, chicken breasts tray, chicken thighs tray, Swanson 33%
   less-sodium broth). The user decides whether to keep or remove them.
3. **Next real run:** plan an upcoming week, approve it, then
   `python main.py cart --week <week>`. Every search and add is now
   logged at INFO (`tool search_walmart {...} -> ...`), so read the log
   afterward to check the open item below.

---

## Open item: in-cart detection on search tiles is unverified

**What's done:** `_add_to_cart` now treats a quantity stepper
(`data-testid="quantity-stepper-inc-button"` / aria-label
`"Increase quantity <name>, Current Quantity N"`) as "already in cart".
Those selectors come from the **cart page**. They're unit-tested but haven't
been seen on a **search-result tile**.

**Why it's blocked:** verifying it means adding a product to the cart and
then searching for it again. That's a real change to the user's cart, and
auto mode blocks ad-hoc cart changes (removals were denied on 2026-09-28 as
"Real-World Transactions"). Only `main.py cart` runs change the cart.

**Contradictory evidence to resolve:**
- In the 2026-09-28 live run, the second search that hit GV Whole Milk, right
  after the filler had added it, had no Add button. So a search tile *can*
  reflect cart state.
- An hour later, on a fresh search, the same in-cart GV milk tile showed a
  normal **"Add"** button. So Walmart does *not* always show cart state on
  tiles.

**Risk:** if tiles show "Add" for items already in the cart, a rerun can
**double-add** instead of skipping. The system prompt's skip logic only
helps if the tile tells the agent the item is already there.

**How to verify on the next real run:** make sure the grocery list includes
something that's already in the cart, then grep the log for that item's
`add_to_cart` line. `Already in cart (qty N)` means the fix works. `Added to
cart` means it was double-added: check the cart quantity. If it double-adds,
a possible fix is to read the cart page once at the start of the run and
pass its contents to the agent as "already in cart".

---

## Walmart human check ("Robot or human? Press & Hold")

The filler does **not** try to bypass it. That would mean defeating
Walmart's bot detection, which is against their terms and gets accounts
flagged. Instead (commit `7a079b5`), when a search hits the check, it prints
a prompt asking the user to press and hold in the Chrome window, waits up
to `walmart_human_check_timeout_s` (default 180, settable in config), and
then continues. If nobody solves it, it tells the agent to stop and report the
remaining items as not found, instead of re-searching into the wall. Live-verified.

How to make it show up less often, legitimately:
- Use the same logged-in debug profile every time. Don't start it with a
  fresh `--user-data-dir`, because a new profile has no history with Walmart.
- Fewer searches per run. The agent searched 27 times for 9 items when
  searches were failing. With the wall now detected, that should drop to
  about 1–2 per item. If it doesn't, tighten the system prompt.
- Don't run the filler several times back to back.

---

## Fixed today (each its own commit)

1. `7b0bc75`: the finish_cart report crashed with `CartItem missing 'status'`
   after every item was already added. The model left out the field the
   schema marks required, and the API doesn't enforce that. Status now comes
   from the bucket key.
2. `4042cf5`: add_to_cart clicked one product but reported another.
   Displayed indices have gaps (tiles without titles are skipped), and the
   lookup used list position. The agent then "corrected" by adding more,
   giving 4 milks for 2 milk lines. The fix clicks by `data-item-id`. Also
   logs every tool call.
3. `7a079b5`: detects the human check instead of stalling for 30s (it was
   9 of 27 searches in one run). Detects the in-cart stepper. Clean product
   names and real prices (the `itemprop=price` element is gone).

Tests: 366 passed, 16 subtests. The one remaining SyntaxWarning (line 1 of
`cart_filler.py`) was already there before these changes.

---

## Cleanup chores (unchanged from last session)

- `~/.zshrc`'s `chrome-debug` alias still uses the broken default-profile
  command. The user needs to edit it by hand (Claude can't write dotfiles)
  to match the command in "Start here" above.
- `README.md` "First-time Chrome setup" (~line 303) and
  `docs/walmart-cart-next-steps.md` still describe the broken approach.
  Now that the cart flow is proven, update both to the dedicated-profile
  command. Also document the human-check prompt and
  `walmart_human_check_timeout_s`.
- Backlog in `docs/next-steps.md`: re-evaluate "Walmart quantity-aware
  search". Prices now reach the agent, but it still picks one package per
  line with no reasoning about quantity.
