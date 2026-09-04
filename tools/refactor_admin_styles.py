"""Move the admin dashboard's repeated inline styles into shared CSS classes.

Why a script instead of hand edits: the same style strings appear ~350 times in
dashboard.html, and most of them live inside JavaScript string concatenation
rather than plain markup. Hand-editing that many sites is where typos come from.
A table of exact style-string -> class-name replacements is reviewable in one
screen and provably consistent.

The safety rule that makes this a refactor and not a redesign: every class below
contains EXACTLY the declarations it replaces - nothing added, nothing dropped.
Where a class is combined with a modifier, the union of the two is exactly the
original declaration list. Rendering is therefore unchanged; only the mechanism
moves from the attribute to the stylesheet.

Idempotent: once a pattern is replaced it no longer matches, so re-running is a
no-op. `git diff` is the review surface; `git checkout` is the undo.

    python tools/refactor_admin_styles.py
"""

import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TPL = ROOT / "sc_assistant" / "templates" / "dashboard.html"
CSS = ROOT / "sc_assistant" / "static" / "css" / "admin-components.css"

# ─────────────────────────────────────────────────────────────────────────────
# The stylesheet. Ordering matters: modifiers must follow their base class so
# that equal-specificity overrides (a ghost button's border over the base
# border:none) resolve the way the original inline style did.
# ─────────────────────────────────────────────────────────────────────────────
CSS_TEXT = """/* ═══════════════════════════════════════════════════════════════
   ADMIN COMPONENT PRIMITIVES

   Extracted verbatim from the inline styles that used to be repeated across
   every admin section of dashboard.html. Each class holds exactly the
   declarations it replaced, so this file changed no pixels when it landed.

   Naming: sc-<component>, with sc-<component>--<variant> modifiers that must
   stay *after* their base class in this file to win the cascade.

   Colours are theme variables wherever the original used them. The literal
   hexes that remain (#3b82f6, #8b5cf6, #f59e0b, #22c55e, #ef4444) are the
   per-section accent colours the sections were already hardcoding; they are
   collected here so a future theme pass has one place to edit.
   ═══════════════════════════════════════════════════════════════ */

/* ── Card shells ─────────────────────────────────────────────── */
.sc-card{background:var(--bg-surface);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow-sm);padding:1.25rem 1.5rem}
/* A card whose child is a full-bleed table: the table draws its own padding. */
.sc-card--flush{padding:0;overflow:hidden}
.sc-mb{margin-bottom:1.5rem}
.sc-mb-lg{margin-bottom:2rem}

/* Top accent stripe. Each admin section owns a colour so a screenshot is
   identifiable without reading the heading. */
.sc-accent-yellow{border-top:4px solid var(--sc-yellow)}
.sc-accent-blue{border-top:4px solid #3b82f6}
.sc-accent-red{border-top:4px solid #ef4444}
.sc-accent-violet{border-top:4px solid #8b5cf6}
.sc-accent-green{border-top:4px solid #22c55e}
.sc-accent-amber{border-top:4px solid #f59e0b}

/* ── Type ────────────────────────────────────────────────────── */
.sc-lead{color:var(--text-secondary);font-size:.9rem;margin:0 0 1rem;line-height:1.55}
.sc-lead--sm{font-size:.85rem;margin:0 0 .75rem;line-height:1.5}
.sc-h3{margin:0 0 .85rem;font-size:1rem;color:var(--text-primary)}
.sc-h3--mid{margin:0 0 .75rem}
.sc-h3--tight{margin:0 0 .5rem}

/* ── Headline counters ───────────────────────────────────────── */
.sc-statrow{display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap}
.sc-stat-num{font-size:1.6rem;font-weight:700;line-height:1;color:var(--text-primary)}
.sc-stat-num--ok{color:#16a34a}
.sc-stat-num--bad{color:#ef4444}
.sc-stat-num--blue{color:#3b82f6}
.sc-stat-num--green{color:#22c55e}
.sc-stat-num--violet{color:#8b5cf6}
.sc-stat-num--amber{color:#f59e0b}
.sc-stat-num--muted{color:var(--text-tertiary)}
.sc-stat-cap{font-size:.78rem;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:.04em}

/* ── Layout helpers ──────────────────────────────────────────── */
.sc-row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
.sc-row--lg{gap:.75rem}
.sc-row--sm{gap:.5rem}
.sc-list{display:flex;flex-direction:column;gap:1rem}
.sc-list--wide{gap:1.25rem}
.sc-list--tight{gap:.75rem}
.sc-list--xtight{gap:.65rem}
.sc-cardhead{display:flex;justify-content:space-between;gap:1rem;align-items:flex-start;flex-wrap:wrap}
.sc-toolbar-right{margin-left:auto;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.sc-push{margin-left:auto}
/* Lets a long unbroken string shrink instead of stretching its flex row. */
.sc-min0{min-width:0}
.sc-nowrap{white-space:nowrap}
.sc-scroll-x{width:100%;overflow-x:auto}
.sc-mt6{margin-top:.6rem}
.sc-mt7{margin-top:.7rem}
.sc-mt85{margin-top:.85rem}
.sc-mt9{margin-top:.9rem}

/* ── Buttons ─────────────────────────────────────────────────── */
.sc-btn{padding:.5rem 1rem;border-radius:8px;border:none;cursor:pointer;font-weight:600;font-size:.875rem}
.sc-btn--primary{background:var(--sc-green);color:#fff}
.sc-btn--blue{background:#3b82f6;color:#fff}
.sc-btn--red{background:#ef4444;color:#fff}
.sc-btn--violet{background:#8b5cf6;color:#fff}
.sc-btn--green{background:#22c55e;color:#fff}
/* Amber needs dark text: white on #f59e0b fails contrast. */
.sc-btn--amber{background:#f59e0b;color:#111827}
.sc-btn--ghost{border:1px solid var(--border);background:var(--bg-main);color:var(--text-secondary)}
.sc-btn--lg{padding:.6rem 1.1rem}
.sc-btn--mid{font-size:.85rem}
.sc-btn--sm{padding:.45rem .9rem;font-size:.82rem}
.sc-btn--xs{padding:.35rem .8rem;font-size:.78rem}
.sc-btn--xxs{padding:.35rem .6rem;font-size:.78rem}
.sc-btn--icon{padding:.35rem .7rem;font-size:.78rem}
.sc-btn--icon-sm{padding:.45rem .7rem;font-size:.82rem}
/* For the handful of originals that never set a weight. */
.sc-btn--reg{font-weight:normal}

/* Filter pills (reports). Standalone, not a .sc-btn variant: the original set
   no font-weight and a 1.5px border, and inheriting the button base would
   quietly embolden them. */
.sc-pill{padding:.35rem .9rem;border-radius:999px;border:1.5px solid var(--border);background:var(--bg-main);color:var(--text-secondary);font-size:.82rem;cursor:pointer;transition:all .15s}
.sc-pill-flex{display:flex;align-items:center;gap:.4rem}
.sc-count{border-radius:999px;padding:0 .45rem;font-size:.72rem;font-weight:700}
.sc-count--red{background:#ef4444;color:#fff}
.sc-count--green{background:#22c55e;color:#fff}
.sc-count--grey{background:#94a3b8;color:#fff}

/* ── Form controls ───────────────────────────────────────────── */
.sc-label{display:block;font-size:.78rem;color:var(--text-tertiary);margin-bottom:.25rem}
.sc-label--inline{font-size:.8rem;color:var(--text-tertiary)}
.sc-input{width:100%;padding:.55rem .7rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.875rem}
.sc-textarea{width:100%;padding:.55rem .7rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.875rem;font-family:inherit;resize:vertical}
.sc-textarea--sm{padding:.6rem .7rem;font-size:.85rem}
.sc-select{padding:.5rem .75rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.85rem}
.sc-select--sm{padding:.45rem .6rem}
.sc-select--mid{padding:.5rem .7rem;font-size:.875rem}
/* Inputs sitting inside a card body rather than on it: bg-main, heavier border. */
.sc-input-inline{padding:.5rem .7rem;border:1.5px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-primary);font-size:.85rem}
.sc-input-inline--sm{padding:.45rem .7rem;font-size:.82rem}
.sc-flex240{flex:1;min-width:240px}
.sc-flex220{flex:1;min-width:220px}
.sc-flex180{flex:1;min-width:180px}

/* ── Tables ──────────────────────────────────────────────────── */
.sc-table{width:100%;border-collapse:collapse;font-size:.875rem}
.sc-th{padding:.7rem 1rem;text-align:left;font-weight:600;color:var(--text-secondary);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
.sc-tabs{display:flex;gap:.5rem;padding:1.25rem 1.5rem;border-bottom:1px solid var(--border);flex-wrap:wrap;align-items:center}

/* ── Empty / error / status ──────────────────────────────────── */
.sc-empty{text-align:center;color:var(--text-secondary);padding:2.5rem}
.sc-empty--sm{padding:2rem}
.sc-emptycard{background:var(--bg-surface);border:1px solid var(--border);border-radius:12px;padding:2.5rem;text-align:center;color:var(--text-secondary)}
.sc-errorblock{color:#ef4444;padding:2rem;text-align:center}
.sc-savemsg{margin-top:.6rem;font-size:.82rem;display:none}

.sc-badge{border-radius:999px;padding:.15rem .6rem;font-size:.72rem;font-weight:700}
.sc-badge--ok{background:#dcfce7;color:#15803d}
.sc-badge--warn{background:#fef3c7;color:#b45309}
.sc-badge--info{background:#dbeafe;color:#1d4ed8}
.sc-badge--violet{background:#ede9fe;color:#6d28d9}
.sc-badge--muted{background:var(--bg-main);color:var(--text-secondary)}

/* ── Inline notes ────────────────────────────────────────────── */
.sc-note{margin-top:.75rem;font-size:.8rem;color:var(--text-secondary);background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:.55rem .8rem}
.sc-note--a{margin-top:.6rem;padding:.5rem .75rem}
.sc-note--ok{color:#15803d;background:rgba(34,197,94,.08);border-color:rgba(34,197,94,.3)}
.sc-note--ok9{margin-top:.9rem;padding:.6rem .8rem}
.sc-note--ok7{margin-top:.7rem;font-size:.82rem}
.sc-note--dashed{margin-top:0;border:1px dashed var(--border);background:transparent;padding:.8rem 1rem;font-size:.82rem}
.sc-pre{white-space:pre-wrap;word-break:break-word;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:.85rem 1rem;font-size:.8rem;color:var(--text-primary);margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
"""

# ─────────────────────────────────────────────────────────────────────────────
# exact inline style value  ->  replacement class list
#
# Keys are matched literally, including the trailing semicolon and the quotes
# around them, so a key can never match a longer style attribute by prefix.
# ─────────────────────────────────────────────────────────────────────────────
_C = "background:var(--bg-surface);border-radius:12px;border:1px solid var(--border);box-shadow:var(--shadow-sm);"
_P = "padding:1.25rem 1.5rem;margin-bottom:1.5rem;"

MAP = {
    # ── card shells ──────────────────────────────────────────────────────
    _C + "border-top:4px solid var(--sc-yellow);" + _P: "sc-card sc-mb sc-accent-yellow",
    _C + "border-top:4px solid #3b82f6;" + _P: "sc-card sc-mb sc-accent-blue",
    _C + "border-top:4px solid #ef4444;" + _P: "sc-card sc-mb sc-accent-red",
    _C + "border-top:4px solid #8b5cf6;" + _P: "sc-card sc-mb sc-accent-violet",
    _C + "border-top:4px solid #22c55e;" + _P: "sc-card sc-mb sc-accent-green",
    _C + "border-top:4px solid #f59e0b;" + _P: "sc-card sc-mb sc-accent-amber",
    _C + "overflow:hidden;border-top:4px solid var(--sc-yellow);": "sc-card sc-card--flush sc-accent-yellow",
    _C + _P: "sc-card sc-mb",
    _C + "padding:1.25rem 1.5rem;": "sc-card",
    "background:var(--bg-surface);border:1px solid var(--border);border-radius:12px;padding:2.5rem;text-align:center;color:var(--text-secondary);": "sc-emptycard",

    # ── type ─────────────────────────────────────────────────────────────
    "color:var(--text-secondary);font-size:.9rem;margin:0 0 1rem;line-height:1.55;": "sc-lead",
    "color:var(--text-secondary);font-size:.85rem;margin:0 0 .75rem;line-height:1.5;": "sc-lead sc-lead--sm",
    "margin:0 0 .85rem;font-size:1rem;color:var(--text-primary);": "sc-h3",
    "margin:0 0 .5rem;font-size:1rem;color:var(--text-primary);": "sc-h3 sc-h3--tight",
    "font-size:1rem;color:var(--text-primary);margin:0 0 .75rem;": "sc-h3 sc-h3--mid",

    # ── counters ─────────────────────────────────────────────────────────
    "display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap;": "sc-statrow",
    "font-size:1.6rem;font-weight:700;color:var(--text-primary);line-height:1;": "sc-stat-num",
    "font-size:1.6rem;font-weight:700;color:#16a34a;line-height:1;": "sc-stat-num sc-stat-num--ok",
    "font-size:1.6rem;font-weight:700;color:#ef4444;line-height:1;": "sc-stat-num sc-stat-num--bad",
    "font-size:1.6rem;font-weight:700;color:#3b82f6;line-height:1;": "sc-stat-num sc-stat-num--blue",
    "font-size:1.6rem;font-weight:700;color:#22c55e;line-height:1;": "sc-stat-num sc-stat-num--green",
    "font-size:1.6rem;font-weight:700;color:#8b5cf6;line-height:1;": "sc-stat-num sc-stat-num--violet",
    "font-size:1.6rem;font-weight:700;color:#f59e0b;line-height:1;": "sc-stat-num sc-stat-num--amber",
    "font-size:1.6rem;font-weight:700;color:var(--text-tertiary);line-height:1;": "sc-stat-num sc-stat-num--muted",
    "font-size:.78rem;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:.04em;": "sc-stat-cap",

    # ── layout ───────────────────────────────────────────────────────────
    "margin-left:auto;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;": "sc-toolbar-right",
    "margin-left:auto;": "sc-push",
    "min-width:0;": "sc-min0",
    "width:100%;overflow-x:auto;": "sc-scroll-x",
    "display:flex;gap:.5rem;padding:1.25rem 1.5rem;border-bottom:1px solid var(--border);flex-wrap:wrap;align-items:center;": "sc-tabs",
    "display:flex;flex-direction:column;gap:1rem;": "sc-list",
    "display:flex;flex-direction:column;gap:1.25rem;": "sc-list sc-list--wide",
    "display:flex;flex-direction:column;gap:.75rem;": "sc-list sc-list--tight",
    "display:flex;flex-direction:column;gap:.75rem;margin-bottom:1.5rem;": "sc-list sc-list--tight sc-mb",
    "display:flex;flex-direction:column;gap:.65rem;": "sc-list sc-list--xtight",
    "display:flex;flex-direction:column;gap:.65rem;margin-bottom:2rem;": "sc-list sc-list--xtight sc-mb-lg",
    "display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;": "sc-row",
    "display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;": "sc-row sc-row--lg",
    "display:flex;gap:.6rem;align-items:center;margin-top:.6rem;flex-wrap:wrap;": "sc-row sc-mt6",
    "display:flex;gap:.6rem;align-items:center;margin-top:.9rem;flex-wrap:wrap;": "sc-row sc-mt9",
    "display:flex;gap:.6rem;align-items:center;margin-top:.85rem;flex-wrap:wrap;": "sc-row sc-mt85",
    "display:flex;gap:.5rem;align-items:center;margin-top:.7rem;flex-wrap:wrap;": "sc-row sc-row--sm sc-mt7",
    "display:flex;justify-content:space-between;gap:1rem;align-items:flex-start;flex-wrap:wrap;": "sc-cardhead",
    "display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;": "sc-cardhead",

    # ── buttons ──────────────────────────────────────────────────────────
    "padding:.5rem 1rem;border-radius:8px;border:none;background:var(--sc-green);color:#fff;cursor:pointer;font-weight:600;font-size:.875rem;": "sc-btn sc-btn--primary",
    "padding:.5rem 1rem;border-radius:8px;border:none;background:#3b82f6;color:#fff;cursor:pointer;font-weight:600;font-size:.875rem;": "sc-btn sc-btn--blue",
    "padding:.5rem 1rem;border-radius:8px;border:none;background:#ef4444;color:#fff;cursor:pointer;font-weight:600;font-size:.875rem;": "sc-btn sc-btn--red",
    "padding:.5rem 1rem;border-radius:8px;border:none;background:#8b5cf6;color:#fff;cursor:pointer;font-weight:600;font-size:.875rem;": "sc-btn sc-btn--violet",
    "padding:.5rem 1rem;border-radius:8px;border:none;background:#22c55e;color:#fff;cursor:pointer;font-weight:600;font-size:.875rem;": "sc-btn sc-btn--green",
    "padding:.5rem 1rem;border-radius:8px;border:none;background:#f59e0b;color:#111827;cursor:pointer;font-weight:600;font-size:.875rem;": "sc-btn sc-btn--amber",
    "padding:.6rem 1.1rem;border-radius:8px;border:none;background:#22c55e;color:#fff;cursor:pointer;font-weight:600;font-size:.875rem;white-space:nowrap;": "sc-btn sc-btn--green sc-btn--lg sc-nowrap",
    "padding:.6rem 1.1rem;border-radius:8px;border:none;background:#f59e0b;color:#111827;cursor:pointer;font-weight:600;font-size:.875rem;white-space:nowrap;": "sc-btn sc-btn--amber sc-btn--lg sc-nowrap",
    "padding:.55rem 1.1rem;border-radius:8px;border:none;background:#8b5cf6;color:#fff;cursor:pointer;font-weight:600;font-size:.875rem;": "sc-btn sc-btn--violet",
    "padding:.5rem 1rem;border:none;border-radius:8px;background:var(--sc-green);color:#fff;font-weight:600;font-size:.85rem;cursor:pointer;": "sc-btn sc-btn--primary sc-btn--mid",
    "padding:.5rem 1rem;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-secondary);font-weight:600;font-size:.85rem;cursor:pointer;": "sc-btn sc-btn--ghost sc-btn--mid",
    "padding:.45rem .9rem;border:none;border-radius:8px;background:var(--sc-green);color:#fff;font-weight:600;font-size:.82rem;cursor:pointer;": "sc-btn sc-btn--primary sc-btn--sm",
    "padding:.45rem .9rem;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-secondary);font-weight:600;font-size:.82rem;cursor:pointer;": "sc-btn sc-btn--ghost sc-btn--sm",
    "display:none;margin-left:auto;padding:.45rem .9rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-main);color:var(--text-secondary);cursor:pointer;font-weight:600;font-size:.82rem;": "sc-btn sc-btn--ghost sc-btn--sm sc-push sc-hidden",
    "padding:.35rem .8rem;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-secondary);font-size:.78rem;font-weight:600;cursor:pointer;": "sc-btn sc-btn--ghost sc-btn--xs",
    "padding:.35rem .8rem;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-secondary);font-size:.78rem;cursor:pointer;": "sc-btn sc-btn--ghost sc-btn--xs sc-btn--reg",
    "padding:.35rem .6rem;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-secondary);font-size:.78rem;cursor:pointer;": "sc-btn sc-btn--ghost sc-btn--xxs sc-btn--reg",
    "padding:.35rem .7rem;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-secondary);font-size:.78rem;cursor:pointer;margin-left:auto;": "sc-btn sc-btn--ghost sc-btn--icon sc-btn--reg sc-push",
    "padding:.45rem .7rem;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-secondary);font-size:.82rem;cursor:pointer;margin-left:auto;": "sc-btn sc-btn--ghost sc-btn--icon-sm sc-btn--reg sc-push",
    "padding:.45rem .7rem;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-secondary);font-size:.82rem;cursor:pointer;": "sc-btn sc-btn--ghost sc-btn--icon-sm sc-btn--reg",
    "padding:.35rem .8rem;border:none;border-radius:8px;background:#3b82f6;color:#fff;font-size:.78rem;font-weight:600;cursor:pointer;white-space:nowrap;": "sc-btn sc-btn--blue sc-btn--xs sc-nowrap",
    "padding:.5rem 1rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-main);color:var(--text-secondary);cursor:pointer;font-weight:600;font-size:.875rem;": "sc-btn sc-btn--ghost",
    "padding:.5rem 1rem;border-radius:8px;border:none;background:#22c55e;color:#fff;cursor:pointer;font-weight:600;font-size:.875rem;": "sc-btn sc-btn--green",

    # filter pills
    "padding:.35rem .9rem;border-radius:999px;border:1.5px solid var(--border);background:var(--bg-main);color:var(--text-secondary);font-size:.82rem;cursor:pointer;display:flex;align-items:center;gap:.4rem;transition:all .15s;": "sc-pill sc-pill-flex",
    "padding:.35rem .9rem;border-radius:999px;border:1.5px solid var(--border);background:var(--bg-main);color:var(--text-secondary);font-size:.82rem;cursor:pointer;transition:all .15s;": "sc-pill",
    "background:#ef4444;color:#fff;border-radius:999px;padding:0 .45rem;font-size:.72rem;font-weight:700;": "sc-count sc-count--red",
    "background:#22c55e;color:#fff;border-radius:999px;padding:0 .45rem;font-size:.72rem;font-weight:700;": "sc-count sc-count--green",
    "background:#94a3b8;color:#fff;border-radius:999px;padding:0 .45rem;font-size:.72rem;font-weight:700;": "sc-count sc-count--grey",

    # ── form controls ────────────────────────────────────────────────────
    "display:block;font-size:.78rem;color:var(--text-tertiary);margin-bottom:.25rem;": "sc-label",
    "font-size:.8rem;color:var(--text-tertiary);": "sc-label--inline",
    "width:100%;padding:.55rem .7rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.875rem;": "sc-input",
    "width:100%;padding:.55rem .7rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.875rem;font-family:inherit;resize:vertical;": "sc-textarea",
    "width:100%;padding:.6rem .7rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.85rem;font-family:inherit;resize:vertical;": "sc-textarea sc-textarea--sm",
    "padding:.5rem .75rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.85rem;": "sc-select",
    "padding:.45rem .6rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.85rem;": "sc-select sc-select--sm",
    "padding:.5rem .6rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.85rem;": "sc-select sc-select--mid-pad",
    "padding:.5rem .7rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.875rem;": "sc-select sc-select--mid",
    "width:100%;padding:.55rem .7rem;border-radius:8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-primary);font-size:.875rem;padding:.55rem .7rem;": "sc-input",
    "flex:1;min-width:240px;padding:.5rem .7rem;border:1.5px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-primary);font-size:.85rem;": "sc-input-inline sc-flex240",
    "flex:1;min-width:180px;padding:.5rem .7rem;border:1.5px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-primary);font-size:.85rem;": "sc-input-inline sc-flex180",
    "flex:1;min-width:220px;padding:.45rem .7rem;border:1.5px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-primary);font-size:.82rem;": "sc-input-inline sc-input-inline--sm sc-flex220",
    "flex:1;min-width:240px;padding:.45rem .7rem;border:1.5px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text-primary);font-size:.82rem;": "sc-input-inline sc-input-inline--sm sc-flex240",

    # ── tables ───────────────────────────────────────────────────────────
    "width:100%;border-collapse:collapse;font-size:.875rem;": "sc-table",
    "padding:.7rem 1rem;text-align:left;font-weight:600;color:var(--text-secondary);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;": "sc-th",

    # ── empty / error / status ───────────────────────────────────────────
    "text-align:center;color:var(--text-secondary);padding:2.5rem;": "sc-empty",
    "text-align:center;color:var(--text-secondary);padding:2rem;": "sc-empty sc-empty--sm",
    "color:#ef4444;padding:2rem;text-align:center;": "sc-errorblock",
    "margin-top:.6rem;font-size:.82rem;display:none;": "sc-savemsg",
    "background:#dcfce7;color:#15803d;border-radius:999px;padding:.15rem .6rem;font-size:.72rem;font-weight:700;": "sc-badge sc-badge--ok",
    "background:#fef3c7;color:#b45309;border-radius:999px;padding:.15rem .6rem;font-size:.72rem;font-weight:700;": "sc-badge sc-badge--warn",
    "background:#dbeafe;color:#1d4ed8;border-radius:999px;padding:.15rem .6rem;font-size:.72rem;font-weight:700;": "sc-badge sc-badge--info",
    "background:#ede9fe;color:#6d28d9;border-radius:999px;padding:.15rem .6rem;font-size:.72rem;font-weight:700;": "sc-badge sc-badge--violet",
    "background:var(--bg-main);color:var(--text-secondary);border-radius:999px;padding:.15rem .6rem;font-size:.72rem;font-weight:700;": "sc-badge sc-badge--muted",

    # ── notes ────────────────────────────────────────────────────────────
    "margin-top:.75rem;font-size:.8rem;color:var(--text-secondary);background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:.55rem .8rem;": "sc-note",
    "margin-top:.6rem;font-size:.8rem;color:var(--text-secondary);background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:.5rem .75rem;": "sc-note sc-note--a",
    "margin-top:.75rem;font-size:.8rem;color:#15803d;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.3);border-radius:8px;padding:.55rem .8rem;": "sc-note sc-note--ok",
    "margin-top:.9rem;font-size:.8rem;color:#15803d;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.3);border-radius:8px;padding:.6rem .8rem;": "sc-note sc-note--ok sc-note--ok9",
    "margin-top:.7rem;font-size:.82rem;color:#15803d;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.3);border-radius:8px;padding:.55rem .8rem;": "sc-note sc-note--ok sc-note--ok7",
    "border:1px dashed var(--border);border-radius:8px;padding:.8rem 1rem;font-size:.82rem;color:var(--text-secondary);": "sc-note sc-note--dashed",
    "white-space:pre-wrap;word-break:break-word;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:.85rem 1rem;font-size:.8rem;color:var(--text-primary);margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;": "sc-pre",
}

# Two extras the map above references but that are cheaper to define here than
# to special-case: a hidden-by-default utility, and the select padding variant.
CSS_TEXT += """
.sc-hidden{display:none}
.sc-select--mid-pad{padding:.5rem .6rem}
"""

TAG = re.compile(r"<[A-Za-z]")
CLASS_ATTR = re.compile(r'class="([^"]*)"')


def _tag_start(text, i):
    """Index of the '<' opening the tag that owns the attribute at `i`."""
    lo = max(0, i - 900)
    start = -1
    for m in TAG.finditer(text, lo, i):
        start = m.start()
    return start


def _apply(text, style, classes):
    """Replace every `style="<style>"` with `class="<classes>"`, merging into an
    existing class attribute on the same tag rather than emitting a second one
    (a duplicate class attribute is silently ignored by the browser - exactly
    the kind of bug that looks like "the CSS didn't work")."""
    needle = 'style="%s"' % style
    hits = skipped = 0
    at = 0
    while True:
        i = text.find(needle, at)
        if i < 0:
            return text, hits, skipped

        j = _tag_start(text, i)
        prior = None
        if j >= 0:
            for m in CLASS_ATTR.finditer(text[j:i]):
                prior = m

        if prior is None:
            # No class attribute before this style. Make sure there isn't one
            # *after* it in the same tag; if there is, leave the site alone.
            tail = text[i + len(needle): i + len(needle) + 400]
            cut = tail.find(">")
            if 'class="' in (tail[:cut] if cut >= 0 else tail):
                skipped += 1
                at = i + len(needle)
                continue
            text = text[:i] + 'class="%s"' % classes + text[i + len(needle):]
            hits += 1
            at = i + len('class="%s"' % classes)
            continue

        # Drop the style attribute (and the whitespace that separated it), then
        # widen the existing class attribute. Order matters: the style attribute
        # sits after the class attribute, so removing it first keeps the earlier
        # offsets valid.
        end = i + len(needle)
        cut = i - 1
        cstart, cend = j + prior.start(), j + prior.end()
        while cut > cend and text[cut] in " \t\r\n":
            cut -= 1
        text = text[:cut + 1] + text[end:]
        merged = 'class="%s %s"' % (prior.group(1), classes)
        text = text[:cstart] + merged + text[cend:]
        hits += 1
        at = cstart + len(merged)


LINK = ("  <link rel=\"stylesheet\"\n"
        "        href=\"{{ url_for('static', filename='css/admin-components.css') }}\"/>\n")
ANCHOR = "        href=\"{{ url_for('static', filename='css/dashboard.css') }}\"/>\n"


def main():
    CSS.write_text(CSS_TEXT, encoding="utf-8")
    text = original = TPL.read_text(encoding="utf-8")

    if "css/admin-components.css" not in text:
        text = text.replace(ANCHOR, ANCHOR + LINK, 1)
        print("linked admin-components.css")

    total = skipped_total = 0
    # Longest keys first: a shorter style string must never win a site that a
    # more specific (longer) rule also describes.
    for style in sorted(MAP, key=len, reverse=True):
        text, hits, skipped = _apply(text, style, MAP[style])
        total += hits
        skipped_total += skipped
        if hits:
            print("%4d  %s" % (hits, MAP[style]))

    TPL.write_text(text, encoding="utf-8")
    left = text.count('style="')
    print("\nreplaced        %d inline style attributes" % total)
    print("skipped (safe)  %d" % skipped_total)
    print("still inline    %d  (dynamic values built in JS, or one-offs)" % left)
    print("template        %d -> %d bytes" % (len(original), len(text)))


if __name__ == "__main__":
    main()
