# Guest Login — Patch Instructions

## Files changed

| File | Change |
|------|--------|
| `sc_assistant/auth.py` | New `POST /guest` route |
| `sc_assistant/chat.py` | Guest-aware logic throughout |
| `sc_assistant/templates/auth.html` | "Continue as Guest" button |
| `sc_assistant/templates/chat.html` | Guest info banner |
| `sc_assistant/templates/partials/sidebar.html` | Guest-safe sidebar |

## How to apply

Copy each file from this patch folder into your project, overwriting the originals:

```bash
cp sc_assistant/auth.py             YOUR_PROJECT/sc_assistant/auth.py
cp sc_assistant/chat.py             YOUR_PROJECT/sc_assistant/chat.py
cp sc_assistant/templates/auth.html \
       YOUR_PROJECT/sc_assistant/templates/auth.html
cp sc_assistant/templates/chat.html \
       YOUR_PROJECT/sc_assistant/templates/chat.html
cp sc_assistant/templates/partials/sidebar.html \
       YOUR_PROJECT/sc_assistant/templates/partials/sidebar.html
```

No database migrations, no new dependencies, no config changes needed.

---

## What guests can and cannot do

| Feature | Guest | Registered student |
|---------|-------|--------------------|
| General SC inquiries (policies, programs, fees, etc.) | ✅ | ✅ |
| Personal academic records (grades, GPA, balance) | ❌ | ✅ |
| Conversation history saved | ❌ | ✅ |
| Load / restore past conversations | ❌ | ✅ |
| Submit flagging / reports | ❌ | ✅ |
| Image uploads | ✅ | ✅ |

---

## How it works (summary)

1. **`/guest` POST** — clears session, sets `is_guest=True`, `uid=None`, `role=guest`, redirects to `/chat/`.
2. **`chat.py`** — a `_is_guest()` helper gates DB operations.  
   Guests skip `upsert_conversation`, `list_conversations`, and `get_conversation` entirely.  
   `uid=None` is passed to the LangGraph chain so no student record is injected into the prompt.
3. **`auth.html`** — a "Continue as Guest" button sits below the sign-in form with a short disclaimer.
4. **`chat.html`** — a yellow info banner is rendered only when `user.is_guest` is truthy.
5. **`sidebar.html`** — guests see a "Sign In for Full Access" call-to-action instead of the normal account dropdown.
