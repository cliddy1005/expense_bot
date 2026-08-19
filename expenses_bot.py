#!/usr/bin/env python3
import argparse
import csv
import json
import re
import hashlib
import subprocess
import time
from datetime import date, timedelta, datetime
from pathlib import Path

REPORT_ID = "3802206699041258"
DEFAULT_REPORT_URL = (
    "https://www.expensify.com/report?"
    "param={%22pageReportID%22:%223802206699041258%22,%22keepCollection%22:true}"
)
REPORT_URL = DEFAULT_REPORT_URL

MERCHANT = "Per Diem"
AMOUNT = "15.00"
DESCRIPTION = "Per Diem"

DEFAULT_START_DATE = date(2026, 6, 1)
DEFAULT_END_DATE = date(2026, 6, 30)
START_DATE = DEFAULT_START_DATE
END_DATE = DEFAULT_END_DATE
BATCH_SIZE = 10
STATE_FILE = Path(".expensify_safari_existing_state.json")
ATTACHMENT_FILE = None


def run_osascript(source: str) -> str:
    p = subprocess.run(["osascript", "-e", source], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip())
    return p.stdout.strip()


def safari_exists() -> bool:
    try:
        return run_osascript(
            'tell application "System Events" to return exists process "Safari"'
        ).lower() == "true"
    except Exception:
        return False


def safari_url() -> str:
    return run_osascript(
        'tell application "Safari" to return URL of current tab of front window'
    )


def safari_title() -> str:
    return run_osascript(
        'tell application "Safari" to return name of current tab of front window'
    )


def safari_navigate(url: str) -> None:
    u = url.replace("\\", "\\\\").replace('"', '\\"')
    run_osascript(
        f'tell application "Safari" to set URL of current tab of front window to "{u}"'
    )


def js_escape(js: str) -> str:
    return (
        js.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def safari_js(js: str) -> str:
    enc = js_escape(js)
    return run_osascript(
        f'tell application "Safari" to return do JavaScript "{enc}" '
        'in current tab of front window'
    )


def wait_ready(timeout: int = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if safari_js("document.readyState") in ("interactive", "complete"):
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError("Safari page did not become ready in time.")



def load_expenses_csv(csv_path):
    """
    Load expense rows from CSV.

    Required columns:
      report_url,date,merchant,amount,currency,category,description,receipt_path

    Date must be YYYY-MM-DD.
    Current Multiple-grid automation expects GBP currency.
    """
    path = Path(csv_path).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"ERROR: CSV file does not exist: {path}")

    required = [
        "report_url",
        "date",
        "merchant",
        "amount",
        "currency",
        "category",
        "description",
        "receipt_path",
    ]

    rows = []

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)

        if not reader.fieldnames:
            raise SystemExit("ERROR: CSV has no header row")

        missing = [c for c in required if c not in reader.fieldnames]
        if missing:
            raise SystemExit(
                "ERROR: CSV is missing required column(s): "
                + ", ".join(missing)
            )

        for line_no, raw in enumerate(reader, start=2):
            if not any((v or "").strip() for v in raw.values()):
                continue

            report_url = (raw["report_url"] or "").strip()
            if not report_url.startswith("https://www.expensify.com/report"):
                raise SystemExit(
                    f"ERROR: CSV line {line_no}: invalid report_url"
                )

            try:
                expense_date = date.fromisoformat(
                    (raw["date"] or "").strip()
                )
            except ValueError:
                raise SystemExit(
                    f"ERROR: CSV line {line_no}: date must be YYYY-MM-DD"
                )

            merchant = (raw["merchant"] or "").strip()
            amount = (raw["amount"] or "").strip()
            currency = (raw["currency"] or "").strip().upper()
            category = (raw["category"] or "").strip()
            description = (raw["description"] or "").strip()
            receipt_raw = (raw["receipt_path"] or "").strip()

            if not merchant:
                raise SystemExit(
                    f"ERROR: CSV line {line_no}: merchant is blank"
                )
            if not amount:
                raise SystemExit(
                    f"ERROR: CSV line {line_no}: amount is blank"
                )
            try:
                amount_number = float(amount)
            except ValueError:
                raise SystemExit(
                    f"ERROR: CSV line {line_no}: amount must be numeric"
                )
            if amount_number <= 0:
                raise SystemExit(
                    f"ERROR: CSV line {line_no}: amount must be greater than 0"
                )

            if currency != "GBP":
                raise SystemExit(
                    f"ERROR: CSV line {line_no}: only GBP is currently supported"
                )

            if not category:
                raise SystemExit(
                    f"ERROR: CSV line {line_no}: category is blank"
                )

            receipt_path = None
            if receipt_raw:
                rp = Path(receipt_raw).expanduser().resolve()
                if not rp.is_file():
                    raise SystemExit(
                        f"ERROR: CSV line {line_no}: receipt file does not exist: {rp}"
                    )
                receipt_path = str(rp)

            rows.append({
                "report_url": report_url,
                "date": expense_date,
                "merchant": merchant,
                "amount": f"{amount_number:.2f}",
                "currency": currency,
                "category": category,
                "description": description,
                "receipt_path": receipt_path,
                "csv_line": line_no,
            })

    if not rows:
        raise SystemExit("ERROR: CSV contains no expense rows")

    report_urls = {r["report_url"] for r in rows}
    if len(report_urls) != 1:
        raise SystemExit(
            "ERROR: all rows in one CSV run must use the same report_url"
        )

    return rows



def june_dates():
    out = []
    d = START_DATE
    while d <= END_DATE:
        out.append(d)
        d += timedelta(days=1)
    return out


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i+size]



def expense_state_key(item):
    """
    Return a stable state key.

    CSV rows are keyed by report + full expense details so identical dates on
    different reports do not collide. Legacy date-only mode is keyed by
    report URL + date.
    """
    if isinstance(item, dict):
        payload = {
            "report_url": item.get("report_url", ""),
            "date": item["date"].isoformat(),
            "merchant": item.get("merchant", ""),
            "amount": item.get("amount", ""),
            "currency": item.get("currency", ""),
            "category": item.get("category", ""),
            "description": item.get("description", ""),
            "receipt_path": item.get("receipt_path") or "",
        }
    else:
        payload = {
            "report_url": REPORT_URL,
            "date": item.isoformat(),
        }

    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"expense:{digest}"



def load_state():
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text()).get("saved_dates", []))
    except Exception:
        return set()


def save_state(saved):
    STATE_FILE.write_text(
        json.dumps({"saved_dates": sorted(saved)}, indent=2) + "\n"
    )


def click_any(labels):
    labels_json = json.dumps(labels)
    js = f"""
(() => {{
  const labels = {labels_json};
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0' &&
           r.width > 0 &&
           r.height > 0;
  }};

  const all = [...document.querySelectorAll('body *')].filter(visible);

  const textOf = el => {{
    return norm(
      el.getAttribute('aria-label') ||
      el.getAttribute('title') ||
      el.innerText ||
      el.textContent ||
      ''
    );
  }};

  const isClickable = el => {{
    if (!el || !visible(el)) return false;
    const role = norm(el.getAttribute('role'));
    const cls = norm(String(el.className || ''));
    const tag = el.tagName;
    const cur = getComputedStyle(el).cursor;

    return tag === 'BUTTON' ||
           tag === 'A' ||
           role === 'button' ||
           role === 'menuitem' ||
           role === 'option' ||
           role === 'link' ||
           el.hasAttribute('tabindex') ||
           cur === 'pointer' ||
           cls.includes('button') ||
           cls.includes('menu');
  }};

  const clickableAncestor = el => {{
    let cur = el;
    for (let i = 0; cur && i < 8; i++, cur = cur.parentElement) {{
      if (isClickable(cur)) return cur;
    }}
    return el;
  }};

  const fire = el => {{
    el.scrollIntoView({{block:'center', inline:'center'}});
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;

    try {{ el.focus({{preventScroll:true}}); }} catch (_) {{}}

    for (const type of [
      'pointerover','mouseover','pointerdown','mousedown',
      'pointerup','mouseup','click'
    ]) {{
      try {{
        const Ctor = type.startsWith('pointer') && window.PointerEvent
          ? PointerEvent : MouseEvent;
        el.dispatchEvent(new Ctor(type, {{
          bubbles:true,
          cancelable:true,
          view:window,
          clientX:x,
          clientY:y,
          button:0,
          buttons:type.includes('down') ? 1 : 0
        }}));
      }} catch (_) {{}}
    }}

    try {{ el.click(); }} catch (_) {{}}
  }};

  const ranked = all
    .map(el => {{
      const r = el.getBoundingClientRect();
      return {{
        el,
        text: textOf(el),
        children: el.children.length,
        area: r.width * r.height
      }};
    }})
    .filter(x => x.text)
    .sort((a,b) => {{
      if (a.children !== b.children) return a.children - b.children;
      return a.area - b.area;
    }});

  for (const label of labels) {{
    const wanted = norm(label);

    let hit = ranked.find(x => x.text === wanted);
    if (!hit) hit = ranked.find(x => x.text.startsWith(wanted));
    if (!hit) hit = ranked.find(x => x.text.includes(wanted));

    if (hit) {{
      const target = clickableAncestor(hit.el);
      fire(target);

      return JSON.stringify({{
        ok:true,
        requested:label,
        matchedText:(hit.el.innerText || hit.el.textContent || '').trim().slice(0,200),
        clickedTag:target.tagName,
        clickedRole:target.getAttribute('role') || '',
        clickedClass:String(target.className || '').slice(0,200)
      }});
    }}
  }}

  const nearby = ranked
    .filter(x => {{
      const t = x.text;
      return t.includes('expense') ||
             t.includes('multiple') ||
             t.includes('new') ||
             t.includes('add');
    }})
    .slice(0,60)
    .map(x => ({{
      text:(x.el.innerText || x.el.textContent || '')
        .trim().replace(/\\s+/g,' ').slice(0,180),
      tag:x.el.tagName,
      role:x.el.getAttribute('role') || '',
      cls:String(x.el.className || '').slice(0,120)
    }}));

  return JSON.stringify({{
    ok:false,
    requested:labels,
    nearby
  }});
}})()
"""
    raw = safari_js(js)
    try:
        return json.loads(raw)
    except Exception:
        return {"ok": False, "raw": raw}



def native_click_text(labels, timeout=15):
    """Find text in the webpage, then click it with a real macOS mouse event."""
    labels_json = json.dumps(labels)
    end = time.time() + timeout
    last = None

    while time.time() < end:
        js = f"""
(() => {{
  const labels = {labels_json};
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0' &&
           r.width > 0 &&
           r.height > 0;
  }};

  const textOf = el => norm(
    el.getAttribute('aria-label') ||
    el.getAttribute('title') ||
    el.innerText ||
    el.textContent ||
    ''
  );

  const isClickable = el => {{
    if (!el || !visible(el)) return false;
    const role = norm(el.getAttribute('role'));
    const tag = el.tagName;
    const cls = norm(String(el.className || ''));
    const cur = getComputedStyle(el).cursor;
    return tag === 'BUTTON' ||
           tag === 'A' ||
           role === 'button' ||
           role === 'menuitem' ||
           role === 'option' ||
           role === 'link' ||
           el.hasAttribute('tabindex') ||
           cur === 'pointer' ||
           cls.includes('button') ||
           cls.includes('menu');
  }};

  const clickableAncestor = el => {{
    let cur = el;
    for (let i = 0; cur && i < 8; i++, cur = cur.parentElement) {{
      if (isClickable(cur)) return cur;
    }}
    return el;
  }};

  const ranked = [...document.querySelectorAll('body *')]
    .filter(visible)
    .map(el => {{
      const r = el.getBoundingClientRect();
      return {{
        el,
        text: textOf(el),
        children: el.children.length,
        area: r.width * r.height,
        top: r.top
      }};
    }})
    .filter(x => x.text)
    .sort((a,b) => a.children - b.children || a.area - b.area);

  for (const label of labels) {{
    const wanted = norm(label);

    let hit = null;

    // Special case: Expensify report pages contain two visible "Add Expenses"
    // strings. Prefer the actual large green report-header button.
    if (wanted === 'add expenses') {{
      const preferred = ranked.filter(x => {{
        const cls = norm(String(x.el.className || ''));
        return x.text === wanted &&
               (
                 cls.includes('btn-success-outline') ||
                 cls.includes('btn-lg') ||
                 cls.includes('btn btn-success')
               );
      }});

      if (preferred.length) {{
        preferred.sort((a,b) => a.top - b.top);
        hit = preferred[0];
      }}

      if (!hit) {{
        const exact = ranked.filter(x => x.text === wanted);
        if (exact.length) {{
          exact.sort((a,b) => a.top - b.top);
          hit = exact[0];
        }}
      }}
    }}

    if (!hit) hit = ranked.find(x => x.text === wanted);
    if (!hit) hit = ranked.find(x => x.text.startsWith(wanted));
    if (!hit) hit = ranked.find(x => x.text.includes(wanted));

    if (hit) {{
      const target = clickableAncestor(hit.el);
      const r = target.getBoundingClientRect();

      const chromeY = Math.max(0, window.outerHeight - window.innerHeight);
      const x = Math.round(window.screenX + r.left + r.width / 2);
      const y = Math.round(window.screenY + chromeY + r.top + r.height / 2);

      return JSON.stringify({{
        ok: true,
        requested: label,
        matchedText: (hit.el.innerText || hit.el.textContent || '').trim().slice(0,200),
        x,
        y,
        rect: {{
          left: Math.round(r.left),
          top: Math.round(r.top),
          width: Math.round(r.width),
          height: Math.round(r.height)
        }},
        screen: {{
          screenX: window.screenX,
          screenY: window.screenY,
          outerHeight: window.outerHeight,
          innerHeight: window.innerHeight,
          chromeY
        }}
      }});
    }}
  }}

  return JSON.stringify({{
    ok: false,
    requested: labels,
    body: (document.body.innerText || '').replace(/\\s+/g,' ').slice(0,1400)
  }});
}})()
"""
        try:
            last = json.loads(safari_js(js))
        except Exception as exc:
            last = {"ok": False, "error": str(exc)}

        if last.get("ok"):
            x = int(last["x"])
            y = int(last["y"])
            applescript = (
                'tell application "Safari" to activate\n'
                'delay 0.2\n'
                'tell application "System Events"\n'
                f'    click at {{{x}, {y}}}\n'
                'end tell'
            )
            run_osascript(applescript)
            time.sleep(0.8)
            return last

        time.sleep(0.4)

    raise RuntimeError(
        f"Could not locate {labels} for native click. Last result:\n"
        + json.dumps(last, indent=2)
    )


def wait_for_visible_text(labels, timeout=12):
    labels_json = json.dumps([x.lower() for x in labels])
    js = f"""
(() => {{
  const labels = {labels_json};
  const body = (document.body.innerText || '').toLowerCase();
  return labels.some(x => body.includes(x)) ? 'yes' : 'no';
}})()
"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if safari_js(js) == "yes":
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False

def click_any_wait(labels, timeout=15):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = click_any(labels)
        if last.get("ok"):
            return last
        time.sleep(0.5)
    raise RuntimeError(
        f"Could not find/click {labels}. Last result:\n"
        + json.dumps(last, indent=2)
    )





def log_expensify_ui_state(label):
    """Print useful DOM diagnostics for overlays, dialogs, New Expense matches and tabs."""
    js = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();

  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0' &&
           r.width > 0 &&
           r.height > 0;
  };

  const rect = el => {
    const r = el.getBoundingClientRect();
    return {
      top: Math.round(r.top),
      left: Math.round(r.left),
      width: Math.round(r.width),
      height: Math.round(r.height),
      area: Math.round(r.width * r.height)
    };
  };

  const describe = el => ({
    tag: el.tagName,
    text: norm(el.innerText || el.textContent || '').slice(0,220),
    cls: String(el.className || '').slice(0,220),
    role: el.getAttribute('role') || '',
    aria: el.getAttribute('aria-label') || '',
    id: el.id || '',
    rect: rect(el)
  });

  const all = [...document.querySelectorAll('body *')].filter(visible);

  const newExpense = all
    .filter(el => norm(el.innerText || el.textContent || '') === 'New Expense')
    .map(describe);

  const multiple = all
    .filter(el => norm(el.innerText || el.textContent || '') === 'Multiple')
    .map(describe);

  const addExpensesToReport = all
    .filter(el => norm(el.innerText || el.textContent || '').includes('Add Expenses To Report'))
    .map(describe)
    .sort((a,b) => a.rect.area - b.rect.area)
    .slice(0,20);

  const dialogs = all
    .filter(el => {
      const t = norm(el.innerText || el.textContent || '');
      const role = el.getAttribute('role') || '';
      const cls = String(el.className || '').toLowerCase();
      return role === 'dialog' ||
             cls.includes('modal') ||
             cls.includes('dialog') ||
             (
               t.includes('New Expense') &&
               t.includes('Expense') &&
               t.includes('Distance') &&
               t.includes('Multiple')
             ) ||
             t.startsWith('Add Expenses To Report');
    })
    .map(describe)
    .sort((a,b) => a.rect.area - b.rect.area)
    .slice(0,30);

  const buttons = all
    .filter(el => {
      const t = norm(el.innerText || el.textContent || '');
      const cls = String(el.className || '').toLowerCase();
      const role = el.getAttribute('role') || '';
      return (
        t === 'New Expense' ||
        t === 'Add Expenses' ||
        t === 'Multiple'
      ) && (
        el.tagName === 'BUTTON' ||
        el.tagName === 'A' ||
        role === 'button' ||
        role === 'tab' ||
        cls.includes('btn') ||
        cls.includes('button') ||
        cls.includes('tab')
      );
    })
    .map(describe);

  return JSON.stringify({
    url: location.href,
    title: document.title,
    newExpense,
    multiple,
    addExpensesToReport,
    buttons,
    dialogs,
    bodyExcerpt: norm(document.body.innerText || '').slice(0,2200)
  });
})()
"""
    try:
        state = json.loads(safari_js(js))
    except Exception as exc:
        print(f"\n--- UI STATE: {label} ---")
        print(f"Could not collect DOM state: {exc}")
        return

    print(f"\n--- UI STATE: {label} ---")
    print(f"URL: {state.get('url')}")
    print(f"Title: {state.get('title')}")

    print("\nVisible exact 'New Expense' matches:")
    for i, item in enumerate(state.get("newExpense", []), 1):
        print(f"  [{i}] {json.dumps(item, ensure_ascii=False)}")

    print("\nVisible exact 'Multiple' matches:")
    for i, item in enumerate(state.get("multiple", []), 1):
        print(f"  [{i}] {json.dumps(item, ensure_ascii=False)}")

    print("\nButton-like Add Expenses / New Expense / Multiple matches:")
    for i, item in enumerate(state.get("buttons", []), 1):
        print(f"  [{i}] {json.dumps(item, ensure_ascii=False)}")

    print("\nPossible dialogs / overlays:")
    for i, item in enumerate(state.get("dialogs", []), 1):
        print(f"  [{i}] {json.dumps(item, ensure_ascii=False)}")

    print("\nBody excerpt:")
    print(state.get("bodyExcerpt", ""))
    print("--- END UI STATE ---\n")


def native_activate_text(labels, timeout=15):
    """
    Activate Expensify controls with strict scoping.

    Add Expenses:
      exact green report-header button.

    New Expense:
      exact green button INSIDE the visible "Add Expenses To Report" overlay.
      Never match the "New Expense" heading in the inner modal.

    Multiple:
      exact tab INSIDE the visible inner "New Expense" modal.
    """
    labels_json = json.dumps(labels)
    end = time.time() + timeout
    last = None

    while time.time() < end:
        js = f"""
(() => {{
  const labels = {labels_json};
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0' &&
           r.width > 0 &&
           r.height > 0;
  }};

  const textOf = el => norm(
    el.getAttribute('aria-label') ||
    el.getAttribute('title') ||
    el.innerText ||
    el.textContent ||
    ''
  );

  const rectObj = el => {{
    const r = el.getBoundingClientRect();
    return {{
      top: Math.round(r.top),
      left: Math.round(r.left),
      width: Math.round(r.width),
      height: Math.round(r.height),
      area: Math.round(r.width * r.height)
    }};
  }};

  const fireClick = target => {{
    target.scrollIntoView({{block:'center', inline:'center'}});
    target.focus?.({{preventScroll:true}});

    // IMPORTANT: activate exactly ONCE.
    // Previous versions dispatched a click event and then called .click(),
    // which could open two identical Expensify dialogs.
    try {{
      target.click();
    }} catch (_) {{
      const r = target.getBoundingClientRect();
      target.dispatchEvent(new MouseEvent('click', {{
        bubbles:true,
        cancelable:true,
        view:window,
        clientX:r.left + r.width/2,
        clientY:r.top + r.height/2,
        button:0
      }}));
    }}
  }};

  const wantedLabels = labels.map(norm);

  // ---------------------------------------------------------------
  // 1) Add Expenses: exact green button in report header
  // ---------------------------------------------------------------
  if (wantedLabels.includes('add expenses')) {{
    const candidates = [...document.querySelectorAll(
      '.btn.btn-success-outline.btn-lg, .btn-success-outline, [class*="btn-success"]'
    )]
      .filter(visible)
      .filter(el => textOf(el) === 'add expenses');

    if (candidates.length) {{
      candidates.sort((a,b) =>
        a.getBoundingClientRect().top - b.getBoundingClientRect().top
      );
      const target = candidates[0];
      fireClick(target);

      return JSON.stringify({{
        ok:true,
        requested:'Add Expenses',
        strategy:'strict-green-add-expenses',
        matchedText:(target.innerText || '').trim(),
        targetTag:target.tagName,
        targetClass:String(target.className || ''),
        rect:rectObj(target)
      }});
    }}

    return JSON.stringify({{
      ok:false,
      requested:'Add Expenses',
      strategy:'strict-green-add-expenses',
      message:'Exact green Add Expenses button not found'
    }});
  }}

  // ---------------------------------------------------------------
  // 2) New Expense: green button INSIDE "Add Expenses To Report"
  // ---------------------------------------------------------------
  if (wantedLabels.includes('new expense')) {{
    const all = [...document.querySelectorAll('body *')].filter(visible);

    // Find the smallest visible container that clearly belongs to the
    // "Add Expenses To Report" overlay and includes the green New Expense button.
    const overlayCandidates = all
      .filter(el => {{
        const t = textOf(el);
        return t.includes('add expenses to report') &&
               t.includes('filters') &&
               t.includes('new expense');
      }})
      .map(el => ({{
        el,
        area: el.getBoundingClientRect().width * el.getBoundingClientRect().height
      }}))
      .sort((a,b) => a.area - b.area);

    let overlay = overlayCandidates.length ? overlayCandidates[0].el : null;

    if (!overlay) {{
      return JSON.stringify({{
        ok:false,
        requested:'New Expense',
        strategy:'strict-new-expense-in-add-overlay',
        message:'Could not identify Add Expenses To Report overlay'
      }});
    }}

    const candidates = [...overlay.querySelectorAll(
      '.btn, [class*="btn"], button, [role="button"], a'
    )]
      .filter(visible)
      .filter(el => textOf(el) === 'new expense');

    if (candidates.length) {{
      // Prefer green/success + larger button styles.
      candidates.sort((a,b) => {{
        const ac = norm(String(a.className || ''));
        const bc = norm(String(b.className || ''));

        const score = cls =>
          (cls.includes('success') ? 0 : 20) +
          (cls.includes('btn-lg') ? 0 : 5) +
          (cls.includes('btn') ? 0 : 10);

        const sa = score(ac);
        const sb = score(bc);

        if (sa !== sb) return sa - sb;

        const ra = a.getBoundingClientRect();
        const rb = b.getBoundingClientRect();
        return ra.top - rb.top;
      }});

      const target = candidates[0];
      fireClick(target);

      return JSON.stringify({{
        ok:true,
        requested:'New Expense',
        strategy:'strict-new-expense-in-add-overlay',
        matchedText:(target.innerText || target.textContent || '').trim(),
        targetTag:target.tagName,
        targetClass:String(target.className || ''),
        rect:rectObj(target),
        overlayText:(overlay.innerText || '').replace(/\\s+/g,' ').trim().slice(0,500)
      }});
    }}

    return JSON.stringify({{
      ok:false,
      requested:'New Expense',
      strategy:'strict-new-expense-in-add-overlay',
      message:'Add Expenses To Report overlay found, but exact button was not',
      overlayText:(overlay.innerText || '').replace(/\\s+/g,' ').trim().slice(0,1000)
    }});
  }}

  // ---------------------------------------------------------------
  // 3) Multiple: tab inside topmost New Expense modal only
  // ---------------------------------------------------------------
  if (wantedLabels.includes('multiple') || wantedLabels.includes('create multiple')) {{
    const all = [...document.querySelectorAll('body *')].filter(visible);

    const modalCandidates = all
      .filter(el => {{
        const t = textOf(el);
        return t.includes('new expense') &&
               t.includes('expense') &&
               t.includes('distance') &&
               t.includes('multiple') &&
               (
                 el.getAttribute('role') === 'dialog' ||
                 String(el.className || '').toLowerCase().includes('modal') ||
                 String(el.className || '').toLowerCase().includes('dialog') ||
                 el.querySelectorAll('input').length >= 2
               );
      }})
      .map(el => ({{
        el,
        area: el.getBoundingClientRect().width * el.getBoundingClientRect().height
      }}))
      .sort((a,b) => a.area - b.area);

    let modal = modalCandidates.length ? modalCandidates[0].el : null;

    if (!modal) {{
      return JSON.stringify({{
        ok:false,
        requested:'Multiple',
        strategy:'strict-multiple-in-new-expense-modal',
        message:'Could not identify visible inner New Expense modal'
      }});
    }}

    // Click the actual Multiple anchor, not its LI/UL parent.
    let candidates = [...modal.querySelectorAll('a')]
      .filter(visible)
      .filter(el => textOf(el) === 'multiple');

    // Fallback for a future UI variant where the tab is a button.
    if (!candidates.length) {{
      candidates = [...modal.querySelectorAll('button, [role="tab"]')]
        .filter(visible)
        .filter(el => textOf(el) === 'multiple');
    }}

    if (candidates.length) {{
      // Prefer the Multiple link belonging to the currently foreground dialog.
      // Higher top values in the logs correspond to the foreground dialog here.
      candidates.sort((a,b) => {{
        const ra = a.getBoundingClientRect();
        const rb = b.getBoundingClientRect();
        if (Math.abs(ra.top - rb.top) > 5) return rb.top - ra.top;
        return (ra.width * ra.height) - (rb.width * rb.height);
      }});

      const target = candidates[0];
      fireClick(target);

      return JSON.stringify({{
        ok:true,
        requested:'Multiple',
        strategy:'exact-multiple-anchor',
        matchedText:(target.innerText || target.textContent || '').trim(),
        targetTag:target.tagName,
        targetClass:String(target.className || ''),
        targetRole:target.getAttribute('role') || '',
        rect:rectObj(target),
        modalText:(modal.innerText || '').replace(/\\s+/g,' ').trim().slice(0,800)
      }});
    }}

    return JSON.stringify({{
      ok:false,
      requested:'Multiple',
      strategy:'strict-multiple-in-new-expense-modal',
      message:'New Expense modal found, but Multiple tab was not',
      modalText:(modal.innerText || '').replace(/\\s+/g,' ').trim().slice(0,1200)
    }});
  }}

  return JSON.stringify({{
    ok:false,
    requested:labels,
    message:'No special handler for requested labels'
  }});
}})()
"""
        try:
            last = json.loads(safari_js(js))
        except Exception as exc:
            last = {"ok": False, "error": str(exc)}

        if last.get("ok"):
            time.sleep(0.9)
            return last

        time.sleep(0.4)

    raise RuntimeError(
        f"Could not activate {labels}. Last result:\n"
        + json.dumps(last, indent=2)
    )

def page_text_excerpt(limit=1800):
    js = f"""
(() => (document.body.innerText || '')
    .replace(/\\s+/g,' ')
    .slice(0,{int(limit)}))()
"""
    return safari_js(js)


def open_create_multiple():
    """
    Add Expenses -> green New Expense button -> Multiple tab,
    with verbose DOM logging before and after each transition.
    """
    log_expensify_ui_state("BEFORE Add Expenses")

    print("  Clicking green Add Expenses...")
    r1 = native_activate_text(["Add Expenses"], 15)
    print("  Add Expenses result:")
    print(json.dumps(r1, indent=2))
    time.sleep(1.0)

    log_expensify_ui_state("AFTER Add Expenses")

    if not wait_for_visible_text(["Add Expenses To Report", "New Expense"], 8):
        raise RuntimeError(
            "Add Expenses click did not expose the Add Expenses To Report overlay."
        )

    print("  Clicking GREEN New Expense button inside Add Expenses To Report...")
    r2 = native_activate_text(["New Expense"], 15)
    print("  New Expense result:")
    print(json.dumps(r2, indent=2))
    time.sleep(1.0)

    log_expensify_ui_state("AFTER New Expense")

    # Safety check: there should be only one foreground New Expense dialog.
    duplicate_check_js = r"""
(() => {
  const visible = el => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           r.width > 0 &&
           r.height > 0;
  };

  const dialogs = [...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)
    .map(el => {
      const r = el.getBoundingClientRect();
      return {
        id: el.id || '',
        top: Math.round(r.top),
        left: Math.round(r.left),
        width: Math.round(r.width),
        height: Math.round(r.height)
      };
    });

  return JSON.stringify(dialogs);
})()
"""
    duplicate_dialogs = json.loads(safari_js(duplicate_check_js))
    print(f"  Visible New Expense dialogs: {len(duplicate_dialogs)}")
    if duplicate_dialogs:
        print(json.dumps(duplicate_dialogs, indent=2))

    if len(duplicate_dialogs) > 1:
        raise RuntimeError(
            "More than one visible New Expense dialog was opened. "
            "Stopping before clicking Multiple."
        )

    if not wait_for_visible_text(["Expense", "Distance", "Multiple"], 8):
        raise RuntimeError(
            "New Expense click did not expose the Expense / Distance / Multiple modal."
        )

    print("  Clicking Multiple tab inside the New Expense modal...")
    r3 = native_activate_text(["Multiple"], 15)
    print("  Multiple result:")
    print(json.dumps(r3, indent=2))
    time.sleep(1.2)

    log_expensify_ui_state("AFTER Multiple")

    state_js = r"""
(() => {
  const visible = el => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           r.width > 0 &&
           r.height > 0;
  };

  const norm = s => (s || '').replace(/\s+/g,' ').trim().toLowerCase();

  const dialog = [...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) {
    return JSON.stringify({
      ok:false,
      message:'No visible New Expense dialog'
    });
  }

  const currentMultiple = [...dialog.querySelectorAll('li.current')]
    .some(el => norm(el.innerText || el.textContent) === 'multiple');

  const txt = norm(dialog.innerText || dialog.textContent);

  return JSON.stringify({
    ok: currentMultiple &&
        txt.includes('date') &&
        txt.includes('merchant') &&
        txt.includes('total') &&
        txt.includes('description'),
    currentMultiple,
    hasDate: txt.includes('date'),
    hasMerchant: txt.includes('merchant'),
    hasTotal: txt.includes('total'),
    hasDescription: txt.includes('description'),
    zeroRows: (txt.match(/£0\.00/g) || []).length,
    excerpt: (dialog.innerText || '')
      .replace(/\s+/g,' ')
      .slice(0,1800)
  });
})()
"""
    state = json.loads(safari_js(state_js))

    if not state.get("ok"):
        raise RuntimeError(
            "Multiple was clicked, but the active Multiple grid was not detected.\n"
            + json.dumps(state, indent=2)
        )

    print(
        "  ✓ Multiple grid detected "
        f"(active tab, {state.get('zeroRows', 0)} visible £0.00 rows)"
    )


def open_create_individual():
    """Open Add Expenses -> New Expense and ensure the Expense tab is active."""
    print("  Clicking green Add Expenses...")
    r1 = native_activate_text(["Add Expenses"], 15)
    if not r1 or not r1.get("ok"):
        raise RuntimeError("Could not click Add Expenses: " + json.dumps(r1, indent=2))
    time.sleep(0.8)

    if not wait_for_visible_text(["Add Expenses To Report", "New Expense"], 8):
        raise RuntimeError(
            "Add Expenses click did not expose the Add Expenses To Report overlay."
        )

    print("  Clicking green New Expense...")
    r2 = native_activate_text(["New Expense"], 15)
    if not r2 or not r2.get("ok"):
        raise RuntimeError("Could not click New Expense: " + json.dumps(r2, indent=2))
    time.sleep(0.8)

    if not wait_for_visible_text(["Expense", "Distance", "Multiple"], 8):
        raise RuntimeError("New Expense dialog did not appear.")

    # Click the Expense tab explicitly so every CSV row starts from a known state.
    js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };
  const norm=s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();

  const dialog=[...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) return JSON.stringify({ok:false,message:'No New Expense dialog'});

  const tabs=[...dialog.querySelectorAll('.dialogTabs a, .dialogTabs li, a, [role="tab"]')]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)==='expense');

  if (!tabs.length) {
    return JSON.stringify({ok:false,message:'Expense tab not found'});
  }

  let target=tabs.find(el => el.tagName==='A') || tabs[0];
  target.click();

  return JSON.stringify({
    ok:true,
    tag:target.tagName,
    cls:String(target.className || '')
  });
})()
"""
    result = json.loads(safari_js(js))
    if not result.get("ok"):
        raise RuntimeError("Could not activate Expense tab: " + json.dumps(result, indent=2))

    time.sleep(0.5)
    print("  ✓ Individual Expense form is open.")


def _individual_js_json(js):
    raw = safari_js(js)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def find_individual_input(label_text):
    """Find an input belonging to a visible individual-expense label."""
    label_json = json.dumps(label_text)

    js = f"""
(() => {{
  const wanted={label_json}.trim().toLowerCase();

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  }};

  const norm=s => (s||'')
    .replace(/\\*/g,'')
    .replace(/\\s+/g,' ')
    .trim()
    .toLowerCase();

  const dialog=[...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) return JSON.stringify({{ok:false,message:'No visible dialog'}});

  const labels=[...dialog.querySelectorAll('label, div, span, td')]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)===wanted)
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        right:r.right,
        top:r.top,
        bottom:r.bottom,
        cy:r.top+r.height/2,
        area:r.width*r.height
      }};
    }})
    .sort((a,b)=>a.area-b.area);

  if (!labels.length) {{
    return JSON.stringify({{
      ok:false,
      message:'Label not found',
      wanted
    }});
  }}

  const lab=labels[0];

  const inputs=[...dialog.querySelectorAll(
    'input:not([type="hidden"]), textarea'
  )]
    .filter(visible)
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        top:r.top,
        cy:r.top+r.height/2,
        width:r.width,
        height:r.height,
        type:el.getAttribute('type') || '',
        name:el.getAttribute('name') || '',
        placeholder:el.getAttribute('placeholder') || '',
        value:el.value || ''
      }};
    }})
    .filter(x => x.left > lab.left - 20)
    .filter(x => Math.abs(x.cy-lab.cy) < 45)
    .sort((a,b) => {{
      const ady=Math.abs(a.cy-lab.cy);
      const bdy=Math.abs(b.cy-lab.cy);
      if (ady!==bdy) return ady-bdy;
      return a.left-b.left;
    }});

  if (!inputs.length) {{
    return JSON.stringify({{
      ok:false,
      message:'No input near label',
      wanted,
      label:{{left:lab.left,top:lab.top,cy:lab.cy}}
    }});
  }}

  const hit=inputs[0];

  return JSON.stringify({{
    ok:true,
    label:wanted,
    input:{{
      left:Math.round(hit.left),
      top:Math.round(hit.top),
      cy:Math.round(hit.cy),
      width:Math.round(hit.width),
      height:Math.round(hit.height),
      type:hit.type,
      name:hit.name,
      placeholder:hit.placeholder,
      value:hit.value
    }}
  }});
}})()
"""
    return _individual_js_json(js)



def wait_for_individual_expense_form(timeout_seconds=12.0):
    """
    Wait until the individual Expense form is usable.

    Receipt uploads can temporarily remove/re-render the original New Expense
    dialog. We detect the form by its actual fields rather than a specific
    Expensify CSS class.
    """
    js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const norm=s => (s||'')
    .replace(/\*/g,'')
    .replace(/\s+/g,' ')
    .trim()
    .toLowerCase();

  const candidates=[...document.querySelectorAll(
    '.dialog, .expenseDialog, [role="dialog"], .dialog_wrapper, .dialog_body'
  )]
    .filter(visible)
    .map(el => {
      const txt=norm(el.innerText || el.textContent);

      const score=[
        'merchant',
        'date',
        'total',
        'category',
        'description'
      ].filter(x => txt.includes(x)).length;

      const r=el.getBoundingClientRect();

      return {
        el,
        score,
        area:r.width*r.height,
        cls:String(el.className || ''),
        text:txt.slice(0,500)
      };
    })
    .filter(x => x.score >= 4)
    .sort((a,b) => b.score-a.score || a.area-b.area);

  if (!candidates.length) {
    return JSON.stringify({
      ok:false,
      message:'Individual expense form not visible yet'
    });
  }

  return JSON.stringify({
    ok:true,
    cls:candidates[0].cls,
    score:candidates[0].score,
    text:candidates[0].text
  });
})()
"""

    deadline = time.time() + timeout_seconds
    last = None

    while time.time() < deadline:
        last = _individual_js_json(js)
        if last and last.get("ok"):
            return last
        time.sleep(0.25)

    return last or {
        "ok": False,
        "message": "Timed out waiting for individual expense form",
    }


def _find_visible_individual_form_js():
    """
    JavaScript expression returning the best visible expense-form element.
    Used inside other JS helpers.
    """
    return r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const norm=s => (s||'')
    .replace(/\*/g,'')
    .replace(/\s+/g,' ')
    .trim()
    .toLowerCase();

  return [...document.querySelectorAll(
    '.dialog, .expenseDialog, [role="dialog"], .dialog_wrapper, .dialog_body'
  )]
    .filter(visible)
    .map(el => {
      const txt=norm(el.innerText || el.textContent);
      const score=[
        'merchant','date','total','category','description'
      ].filter(x => txt.includes(x)).length;
      const r=el.getBoundingClientRect();
      return {el,score,area:r.width*r.height};
    })
    .filter(x => x.score >= 4)
    .sort((a,b)=>b.score-a.score || a.area-b.area)[0]?.el || null;
})()
"""



def set_individual_text_field(label_text, value):
    """
    Set Merchant, Total, or Description in the individual form.

    Uses content-based form detection so it still works after receipt upload
    re-renders the Expensify dialog.
    """
    label_json = json.dumps(label_text)
    value_json = json.dumps(str(value))

    js = f"""
(() => {{
  const wanted={label_json}.trim().toLowerCase();
  const value={value_json};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  }};

  const norm=s => (s||'')
    .replace(/\\*/g,'')
    .replace(/\\s+/g,' ')
    .trim()
    .toLowerCase();

  const forms=[...document.querySelectorAll(
    '.dialog, .expenseDialog, [role="dialog"], .dialog_wrapper, .dialog_body'
  )]
    .filter(visible)
    .map(el => {{
      const txt=norm(el.innerText || el.textContent);
      const score=[
        'merchant','date','total','category','description'
      ].filter(x => txt.includes(x)).length;
      const r=el.getBoundingClientRect();
      return {{el,score,area:r.width*r.height}};
    }})
    .filter(x => x.score >= 4)
    .sort((a,b)=>b.score-a.score || a.area-b.area);

  if (!forms.length) {{
    return JSON.stringify({{
      ok:false,
      message:'No visible individual expense form'
    }});
  }}

  const dialog=forms[0].el;

  const labels=[...dialog.querySelectorAll('label, div, span, td')]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)===wanted)
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        top:r.top,
        cy:r.top+r.height/2,
        area:r.width*r.height
      }};
    }})
    .sort((a,b)=>a.area-b.area);

  if (!labels.length) {{
    return JSON.stringify({{
      ok:false,
      message:'Label not found',
      wanted
    }});
  }}

  const lab=labels[0];

  const inputs=[...dialog.querySelectorAll(
    'input:not([type="hidden"]), textarea'
  )]
    .filter(visible)
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        cy:r.top+r.height/2,
        top:r.top,
        width:r.width,
        height:r.height
      }};
    }})
    .filter(x => x.left > lab.left-20)
    .filter(x => Math.abs(x.cy-lab.cy) < 45)
    .sort((a,b) => {{
      const ady=Math.abs(a.cy-lab.cy);
      const bdy=Math.abs(b.cy-lab.cy);
      if (ady!==bdy) return ady-bdy;
      return a.left-b.left;
    }});

  if (!inputs.length) {{
    return JSON.stringify({{
      ok:false,
      message:'Input not found near label',
      wanted
    }});
  }}

  const input=inputs[0].el;
  input.focus();

  const proto = input.tagName === 'TEXTAREA'
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;

  const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;
  if (setter) setter.call(input,value);
  else input.value=value;

  input.dispatchEvent(new Event('input',{{bubbles:true}}));
  input.dispatchEvent(new Event('change',{{bubbles:true}}));
  input.blur();

  return JSON.stringify({{
    ok:true,
    label:wanted,
    value:input.value
  }});
}})()
"""
    return _individual_js_json(js)



def set_individual_date_direct(target_date):
    """
    Set the individual Expense Date field directly as YYYY-MM-DD.

    This is more reliable after receipt upload because Expensify can re-render
    the datepicker/calendar state. The underlying Date input accepts ISO dates.
    """
    iso_value = target_date.isoformat()
    iso_json = json.dumps(iso_value)

    js = f"""
(() => {{
  const wanted={iso_json};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  }};

  const norm=s => (s||'')
    .replace(/\\*/g,'')
    .replace(/\\s+/g,' ')
    .trim()
    .toLowerCase();

  const forms=[...document.querySelectorAll(
    '.dialog, .expenseDialog, [role="dialog"]'
  )]
    .filter(visible)
    .filter(el => {{
      const txt=norm(el.innerText || el.textContent);
      return (
        txt.includes('merchant') &&
        txt.includes('date') &&
        txt.includes('total') &&
        txt.includes('category')
      );
    }});

  if (!forms.length) {{
    return JSON.stringify({{
      ok:false,
      message:'No visible individual expense form'
    }});
  }}

  const dialog=forms[0];

  const labels=[...dialog.querySelectorAll('label, div, span, td')]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)==='date')
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        cy:r.top+r.height/2,
        area:r.width*r.height
      }};
    }})
    .sort((a,b)=>a.area-b.area);

  if (!labels.length) {{
    return JSON.stringify({{
      ok:false,
      message:'Date label not found'
    }});
  }}

  const lab=labels[0];

  const inputs=[...dialog.querySelectorAll(
    'input:not([type="hidden"])'
  )]
    .filter(visible)
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        cy:r.top+r.height/2,
        width:r.width,
        height:r.height,
        type:el.getAttribute('type') || '',
        name:el.getAttribute('name') || '',
        value:el.value || ''
      }};
    }})
    .filter(x =>
      x.left > lab.left-20 &&
      Math.abs(x.cy-lab.cy)<45
    )
    .sort((a,b)=>{{
      const ad=Math.abs(a.cy-lab.cy);
      const bd=Math.abs(b.cy-lab.cy);
      if (ad!==bd) return ad-bd;
      return a.left-b.left;
    }});

  if (!inputs.length) {{
    return JSON.stringify({{
      ok:false,
      message:'Date input not found'
    }});
  }}

  const input=inputs[0].el;

  input.focus();

  const setter=Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    'value'
  )?.set;

  if (setter) setter.call(input,wanted);
  else input.value=wanted;

  input.dispatchEvent(new Event('input',{{bubbles:true}}));
  input.dispatchEvent(new Event('change',{{bubbles:true}}));
  input.dispatchEvent(new Event('blur',{{bubbles:true}}));

  return JSON.stringify({{
    ok:true,
    requested:wanted,
    value:input.value,
    type:input.getAttribute('type') || '',
    name:input.getAttribute('name') || ''
  }});
}})()
"""

    result = _individual_js_json(js)

    if not result or not result.get("ok"):
        return result

    # Verify after a short delay in case Expensify normalizes/re-renders it.
    time.sleep(0.2)

    verify_js = f"""
(() => {{
  const wanted={iso_json};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           r.width>0 &&
           r.height>0;
  }};

  const vals=[...document.querySelectorAll(
    '.dialog input:not([type="hidden"]), .expenseDialog input:not([type="hidden"]), [role="dialog"] input:not([type="hidden"])'
  )]
    .filter(visible)
    .map(el => el.value || '');

  return JSON.stringify({{
    ok:vals.includes(wanted),
    wanted,
    values:vals.slice(0,30)
  }});
}})()
"""

    verify = _individual_js_json(verify_js)

    if not verify or not verify.get("ok"):
        return {
            "ok": False,
            "message": "Date was set but did not remain committed",
            "set_result": result,
            "verification": verify,
        }

    return {
        "ok": True,
        "date": iso_value,
        "set_result": result,
        "verification": verify,
    }



def select_individual_date(target_date):
    """Open the Date calendar and click the requested day."""
    target_iso = target_date.isoformat()

    # First click the Date input.
    click_js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };
  const norm=s => (s||'')
    .replace(/\*/g,'')
    .replace(/\s+/g,' ')
    .trim()
    .toLowerCase();

  const dialog=[...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) return JSON.stringify({ok:false,message:'No dialog'});

  const labs=[...dialog.querySelectorAll('label, div, span, td')]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)==='date')
    .map(el => {
      const r=el.getBoundingClientRect();
      return {el,left:r.left,cy:r.top+r.height/2,area:r.width*r.height};
    })
    .sort((a,b)=>a.area-b.area);

  if (!labs.length) return JSON.stringify({ok:false,message:'Date label not found'});

  const lab=labs[0];

  const inputs=[...dialog.querySelectorAll('input:not([type="hidden"])')]
    .filter(visible)
    .map(el => {
      const r=el.getBoundingClientRect();
      return {el,left:r.left,cy:r.top+r.height/2};
    })
    .filter(x => x.left > lab.left-20 && Math.abs(x.cy-lab.cy)<45)
    .sort((a,b)=>Math.abs(a.cy-lab.cy)-Math.abs(b.cy-lab.cy));

  if (!inputs.length) return JSON.stringify({ok:false,message:'Date input not found'});

  inputs[0].el.click();
  inputs[0].el.focus();

  return JSON.stringify({ok:true});
})()
"""
    first = _individual_js_json(click_js)
    if not first or not first.get("ok"):
        return first

    time.sleep(0.25)

    target_month = target_date.month
    target_year = target_date.year
    target_day = target_date.day
    month_names = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december"
    ]

    for _ in range(30):
        read_js = f"""
(() => {{
  const months={json.dumps(month_names)};
  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>100 &&
           r.height>80;
  }};
  const norm=s => (s||'').replace(/\\s+/g,' ').trim().toLowerCase();

  const cands=[...document.querySelectorAll('body *')]
    .filter(visible)
    .map(el => {{
      const r=el.getBoundingClientRect();
      const txt=norm(el.innerText || el.textContent);
      const mi=months.findIndex(m=>txt.includes(m));
      const ym=txt.match(/20\\d{{2}}/);
      const weekdays=['su','mo','tu','we','th','fr','sa']
        .filter(d=>new RegExp(`(^|\\\\s)${{d}}(\\\\s|$)`).test(txt)).length;
      return {{
        el,txt,mi,year:ym?parseInt(ym[0],10):null,
        weekdays,area:r.width*r.height
      }};
    }})
    .filter(x=>x.mi>=0 && x.year!==null && x.weekdays>=4)
    .sort((a,b)=>a.area-b.area);

  if (!cands.length) return JSON.stringify({{ok:false,message:'Calendar not found'}});

  const c=cands[0];

  return JSON.stringify({{
    ok:true,
    month:c.mi+1,
    year:c.year
  }});
}})()
"""
        state = _individual_js_json(read_js)
        if not state or not state.get("ok"):
            return state

        cm = state["month"]
        cy = state["year"]

        if cm == target_month and cy == target_year:
            day_js = f"""
(() => {{
  const day={target_day};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  }};

  const months={json.dumps(month_names)};
  const norm=s => (s||'').replace(/\\s+/g,' ').trim().toLowerCase();

  const cands=[...document.querySelectorAll('body *')]
    .filter(el => {{
      if (!visible(el)) return false;
      const r=el.getBoundingClientRect();
      if (r.width<100 || r.height<80) return false;
      const txt=norm(el.innerText || el.textContent);
      return months.some(m=>txt.includes(m)) && /20\\d{{2}}/.test(txt);
    }})
    .sort((a,b)=>{{
      const ar=a.getBoundingClientRect(), br=b.getBoundingClientRect();
      return ar.width*ar.height-br.width*br.height;
    }});

  if (!cands.length) return JSON.stringify({{ok:false,message:'Calendar not found'}});

  const cal=cands[0];

  const links=[...cal.querySelectorAll('a.ui-state-default, a')]
    .filter(visible)
    .filter(a => (a.innerText || a.textContent || '').trim()===String(day))
    .filter(a => !a.closest('.ui-datepicker-other-month'));

  if (!links.length) {{
    return JSON.stringify({{ok:false,message:'Requested day not found',day}});
  }}

  links[0].click();
  return JSON.stringify({{ok:true,day}});
}})()
"""
            return _individual_js_json(day_js)

        current_index = cy * 12 + (cm - 1)
        target_index = target_year * 12 + (target_month - 1)
        direction = "next" if target_index > current_index else "prev"

        nav_js = f"""
(() => {{
  const dir={json.dumps(direction)};
  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           r.width>0 &&
           r.height>0;
  }};

  const sels = dir==='next'
    ? ['.ui-datepicker-next','a[title*="Next"]','button[aria-label*="Next"]']
    : ['.ui-datepicker-prev','a[title*="Prev"]','button[aria-label*="Prev"]'];

  for (const sel of sels) {{
    const el=[...document.querySelectorAll(sel)].find(visible);
    if (el) {{
      el.click();
      return JSON.stringify({{ok:true,dir}});
    }}
  }}

  return JSON.stringify({{ok:false,message:'Calendar navigation control not found',dir}});
}})()
"""
        nav = _individual_js_json(nav_js)
        if not nav or not nav.get("ok"):
            return nav

        time.sleep(0.15)

    return {"ok": False, "message": f"Could not reach date {target_iso}"}


def select_individual_category(category):
    """
    Type the Category, click the exact dropdown value, and verify that the
    Category field actually committed the requested value.
    """
    cat_json = json.dumps(category)

    type_js = f"""
(() => {{
  const wanted={cat_json};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  }};

  const norm=s => (s||'')
    .replace(/\\*/g,'')
    .replace(/\\s+/g,' ')
    .trim()
    .toLowerCase();

  const dialog=[...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) {{
    return JSON.stringify({{ok:false,message:'No visible New Expense dialog'}});
  }}

  const labels=[...dialog.querySelectorAll('label, div, span, td')]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)==='category')
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        cy:r.top+r.height/2,
        area:r.width*r.height
      }};
    }})
    .sort((a,b)=>a.area-b.area);

  if (!labels.length) {{
    return JSON.stringify({{ok:false,message:'Category label not found'}});
  }}

  const lab=labels[0];

  const inputs=[...dialog.querySelectorAll('input:not([type="hidden"])')]
    .filter(visible)
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        cy:r.top+r.height/2,
        width:r.width,
        height:r.height
      }};
    }})
    .filter(x => x.left > lab.left-20 && Math.abs(x.cy-lab.cy)<45)
    .sort((a,b)=>Math.abs(a.cy-lab.cy)-Math.abs(b.cy-lab.cy));

  if (!inputs.length) {{
    return JSON.stringify({{ok:false,message:'Category input not found'}});
  }}

  const input=inputs[0].el;

  input.click();
  input.focus();

  const setter=Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,'value'
  )?.set;

  if (setter) setter.call(input,wanted);
  else input.value=wanted;

  input.dispatchEvent(new Event('input',{{bubbles:true}}));
  input.dispatchEvent(new KeyboardEvent('keyup',{{bubbles:true,key:'a'}}));

  return JSON.stringify({{
    ok:true,
    typed:input.value
  }});
}})()
"""

    typed = _individual_js_json(type_js)
    if not typed or not typed.get("ok"):
        return typed

    time.sleep(0.45)

    click_js = f"""
(() => {{
  const wanted={cat_json}.trim().toLowerCase();

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  }};

  const norm=s => (s||'').replace(/\\s+/g,' ').trim().toLowerCase();

  const candidates=[...document.querySelectorAll(
    '.dropdown-menu li, .expensify-dropdown li, [role="option"], [role="menuitem"]'
  )]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)===wanted)
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        tag:el.tagName,
        cls:String(el.className || ''),
        parentCls:String(el.parentElement?.className || ''),
        left:r.left,
        top:r.top,
        width:r.width,
        height:r.height,
        area:r.width*r.height
      }};
    }})
    .sort((a,b)=>a.area-b.area);

  if (!candidates.length) {{
    return JSON.stringify({{
      ok:false,
      message:'Exact Category dropdown value not found',
      wanted
    }});
  }}

  const hit=candidates[0];
  const r=hit.el.getBoundingClientRect();
  const x=r.left+r.width/2;
  const y=r.top+r.height/2;

  // Expensify's dropdown responds more reliably to a real mouse sequence
  // than to element.click() alone.
  hit.el.dispatchEvent(new MouseEvent('mousedown',{{
    bubbles:true,
    cancelable:true,
    clientX:x,
    clientY:y,
    view:window
  }}));

  hit.el.dispatchEvent(new MouseEvent('mouseup',{{
    bubbles:true,
    cancelable:true,
    clientX:x,
    clientY:y,
    view:window
  }}));

  hit.el.dispatchEvent(new MouseEvent('click',{{
    bubbles:true,
    cancelable:true,
    clientX:x,
    clientY:y,
    view:window
  }}));

  try {{ hit.el.click(); }} catch (_) {{}}

  return JSON.stringify({{
    ok:true,
    wanted,
    tag:hit.tag,
    cls:hit.cls,
    parentCls:hit.parentCls,
    centerX:Math.round(x),
    centerY:Math.round(y)
  }});
}})()
"""

    clicked = _individual_js_json(click_js)
    if not clicked or not clicked.get("ok"):
        return clicked

    # Wait for Expensify to commit the dropdown selection.
    verify_js = f"""
(() => {{
  const wanted={cat_json}.trim().toLowerCase();

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  }};

  const norm=s => (s||'')
    .replace(/\\*/g,'')
    .replace(/\\s+/g,' ')
    .trim()
    .toLowerCase();

  const dialog=[...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) {{
    return JSON.stringify({{ok:false,message:'Dialog disappeared'}});
  }}

  const labels=[...dialog.querySelectorAll('label, div, span, td')]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)==='category')
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        cy:r.top+r.height/2,
        area:r.width*r.height
      }};
    }})
    .sort((a,b)=>a.area-b.area);

  if (!labels.length) {{
    return JSON.stringify({{ok:false,message:'Category label not found'}});
  }}

  const lab=labels[0];

  const nearby=[...dialog.querySelectorAll('input, div, span')]
    .filter(visible)
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        value:(el.value || ''),
        text:(el.innerText || el.textContent || ''),
        left:r.left,
        cy:r.top+r.height/2,
        width:r.width,
        height:r.height
      }};
    }})
    .filter(x => x.left > lab.left-20 && Math.abs(x.cy-lab.cy)<50);

  const values=nearby.map(x=>norm(x.value || x.text)).filter(Boolean);
  const committed=values.some(v => v===wanted || v.includes(wanted));

  return JSON.stringify({{
    ok:committed,
    wanted,
    values:values.slice(0,20)
  }});
}})()
"""

    last = None
    for _ in range(20):
        last = _individual_js_json(verify_js)
        if last and last.get("ok"):
            return {
                "ok": True,
                "wanted": category,
                "clicked": clicked,
                "verified": last,
            }
        time.sleep(0.15)

    return {
        "ok": False,
        "message": "Category option was clicked but did not commit",
        "wanted": category,
        "clicked": clicked,
        "verification": last,
    }



def click_individual_receipt_plus():
    """
    Click the actual green + inside .receiptContainer.

    Based on the individual Expense layout, the + center is approximately
    64% across and 55% down the receipt container. We target the deepest
    element under that point rather than clicking the large container itself.
    """
    click_js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const dialog=[...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) {
    return JSON.stringify({ok:false,message:'No visible New Expense dialog'});
  }

  const receipt=[...dialog.querySelectorAll('.receiptContainer')]
    .filter(visible)[0];

  if (!receipt) {
    return JSON.stringify({ok:false,message:'Visible .receiptContainer not found'});
  }

  const rr=receipt.getBoundingClientRect();

  // Actual green + location inside the empty receipt pane.
  const x=rr.left + rr.width * 0.64;
  const y=rr.top  + rr.height * 0.55;

  const probeOffsets = [
    [0,0],
    [-6,0],[6,0],[0,-6],[0,6],
    [-10,-10],[10,-10],[-10,10],[10,10],
    [0,14],[0,-14]
  ];

  const attempts=[];

  for (const [dx,dy] of probeOffsets) {
    const px=x+dx;
    const py=y+dy;
    const deepest=document.elementFromPoint(px,py);

    if (!deepest) continue;

    const r=deepest.getBoundingClientRect();

    attempts.push({
      x:Math.round(px),
      y:Math.round(py),
      tag:deepest.tagName,
      cls:String(deepest.className || ''),
      left:Math.round(r.left),
      top:Math.round(r.top),
      width:Math.round(r.width),
      height:Math.round(r.height)
    });

    // Dispatch on the element actually under the green + location.
    const opts={
      bubbles:true,
      cancelable:true,
      clientX:px,
      clientY:py,
      view:window
    };

    deepest.dispatchEvent(new MouseEvent('mouseover',opts));
    deepest.dispatchEvent(new MouseEvent('mouseenter',opts));
    deepest.dispatchEvent(new MouseEvent('mousedown',opts));
    deepest.dispatchEvent(new MouseEvent('mouseup',opts));
    deepest.dispatchEvent(new MouseEvent('click',opts));

    try { deepest.click(); } catch (_) {}

    // Also try its nearest reasonably-sized ancestor, but never the entire
    // receiptContainer unless the point itself truly resolves to it.
    let p=deepest.parentElement;
    for (let i=0; i<3 && p && p!==receipt; i++,p=p.parentElement) {
      const pr=p.getBoundingClientRect();
      if (pr.width <= 100 && pr.height <= 100) {
        try {
          p.dispatchEvent(new MouseEvent('click',{
            bubbles:true,
            cancelable:true,
            clientX:px,
            clientY:py,
            view:window
          }));
          p.click();
        } catch (_) {}
        break;
      }
    }

    return JSON.stringify({
      ok:true,
      strategy:'receipt-container-plus-point',
      receipt:{
        left:Math.round(rr.left),
        top:Math.round(rr.top),
        width:Math.round(rr.width),
        height:Math.round(rr.height)
      },
      target:{
        x:Math.round(px),
        y:Math.round(py),
        tag:deepest.tagName,
        cls:String(deepest.className || '')
      },
      attempts
    });
  }

  return JSON.stringify({
    ok:false,
    message:'Could not resolve green + target point',
    receipt:{
      left:Math.round(rr.left),
      top:Math.round(rr.top),
      width:Math.round(rr.width),
      height:Math.round(rr.height)
    },
    expected:{
      x:Math.round(x),
      y:Math.round(y)
    },
    attempts
  });
})()
"""

    click_result = _individual_js_json(click_js)
    if not click_result or not click_result.get("ok"):
        return click_result

    verify_js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const norm=s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();

  const matches=[...document.querySelectorAll(
    'button, a, div, [role="button"]'
  )]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)==='import from computer');

  return JSON.stringify({
    ok:matches.length>0,
    count:matches.length
  });
})()
"""

    last = None
    for _ in range(25):
        last = _individual_js_json(verify_js)
        if last and last.get("ok"):
            click_result["menuVisible"] = True
            return click_result
        time.sleep(0.12)

    click_result["ok"] = False
    click_result["message"] = (
        "Green receipt + target was clicked but Import From Computer did not appear"
    )
    click_result["menuCheck"] = last
    return click_result



def click_individual_import_from_computer():
    """Click the exact visible Import From Computer button."""
    js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };
  const norm=s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();

  const matches=[...document.querySelectorAll(
    'button, a, div, [role="button"]'
  )]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)==='import from computer')
    .map(el => {
      const r=el.getBoundingClientRect();
      return {
        el,
        cls:String(el.className || ''),
        tag:el.tagName,
        area:r.width*r.height
      };
    })
    .sort((a,b)=>{
      const ag=/success|green|primary/.test(a.cls.toLowerCase()) ? 0 : 1;
      const bg=/success|green|primary/.test(b.cls.toLowerCase()) ? 0 : 1;
      return ag-bg || a.area-b.area;
    });

  if (!matches.length) {
    return JSON.stringify({
      ok:false,
      message:'Visible Import From Computer button not found'
    });
  }

  matches[0].el.click();

  return JSON.stringify({
    ok:true,
    tag:matches[0].tag,
    cls:matches[0].cls
  });
})()
"""
    last = None
    for _ in range(30):
        last = _individual_js_json(js)
        if last and last.get("ok"):
            return last
        time.sleep(0.1)
    return last or {"ok": False, "message": "Import From Computer did not appear"}


def choose_individual_file(file_path):
    """Choose the receipt using the native macOS Open dialog."""
    p = str(Path(file_path).expanduser().resolve())

    cp = subprocess.run(["pbcopy"], input=p, text=True, capture_output=True)
    if cp.returncode != 0:
        return {"ok": False, "message": "Could not copy receipt path to clipboard"}

    applescript = """
tell application "System Events"
    tell process "Safari"
        set frontmost to true
        delay 0.6
        keystroke "g" using {command down, shift down}
        delay 0.4
        keystroke "v" using {command down}
        delay 0.3
        key code 36
        delay 0.8
        key code 36
        delay 1.2
    end tell
end tell
"""
    try:
        run_osascript(applescript)
    except Exception as e:
        return {"ok": False, "message": str(e), "path": p}

    return {"ok": True, "path": p}


def attach_individual_receipt(file_path):
    """Click receipt + -> Import From Computer -> choose row receipt."""
    plus = click_individual_receipt_plus()
    if not plus or not plus.get("ok"):
        return {"ok": False, "stage": "receipt-plus", "details": plus}

    time.sleep(0.35)

    imp = click_individual_import_from_computer()
    if not imp or not imp.get("ok"):
        return {"ok": False, "stage": "import-from-computer", "details": imp}

    time.sleep(0.45)

    picked = choose_individual_file(file_path)
    if not picked or not picked.get("ok"):
        return {"ok": False, "stage": "file-picker", "details": picked}

    time.sleep(1.2)

    return {
        "ok": True,
        "plus": plus,
        "import": imp,
        "file": picked,
    }


def fill_individual_expense(item):
    """
    Fill one CSV row in the normal single Expense form.

    IMPORTANT ORDER:
      1. For non-Per-Diem expenses, upload and validate the receipt FIRST.
      2. Then fill Merchant / Date / Total / Category / Description.
      3. Save is only allowed later if receipt validation succeeded.

    Per Diem receipts remain optional.
    """
    results = {}

    category_lower = (item.get("category") or "").strip().lower()
    merchant_lower = (item.get("merchant") or "").strip().lower()

    is_per_diem = (
        category_lower == "per diem" or
        merchant_lower == "per diem"
    )

    receipt_path = item.get("receipt_path")

    # ------------------------------------------------------------
    # RECEIPT FIRST
    # ------------------------------------------------------------
    if not is_per_diem:
        if not receipt_path:
            results["receipt"] = {
                "ok": False,
                "message": (
                    "Receipt is required for non-Per-Diem expenses, "
                    "but receipt_path is blank."
                ),
            }
            return {
                "ok": False,
                "field": "receipt-required",
                "debug": results,
            }

        rp = Path(receipt_path).expanduser()
        if not rp.is_file():
            results["receipt"] = {
                "ok": False,
                "message": f"Receipt file does not exist: {rp}",
            }
            return {
                "ok": False,
                "field": "receipt-required",
                "debug": results,
            }

        print("  Uploading required receipt before editing fields...")

        results["receipt"] = attach_individual_receipt(receipt_path)

        if not results["receipt"] or not results["receipt"].get("ok"):
            return {
                "ok": False,
                "field": "receipt",
                "debug": results,
            }

        results["receipt_validated"] = True
        print("  ✓ Receipt uploaded successfully.")
        print("  Waiting for Expense form to finish re-rendering...")

        form_state = wait_for_individual_expense_form(timeout_seconds=15.0)
        results["post_receipt_form"] = form_state

        if not form_state or not form_state.get("ok"):
            return {
                "ok": False,
                "field": "post-receipt-form",
                "debug": results,
            }

        print("  ✓ Expense form is ready. Now filling fields.")
        time.sleep(0.25)

    else:
        # Per Diem receipt is optional.
        if receipt_path:
            print("  Per Diem receipt supplied; uploading it first...")
            results["receipt"] = attach_individual_receipt(receipt_path)

            if not results["receipt"] or not results["receipt"].get("ok"):
                return {
                    "ok": False,
                    "field": "receipt",
                    "debug": results,
                }

            results["receipt_validated"] = True

            form_state = wait_for_individual_expense_form(
                timeout_seconds=15.0
            )
            results["post_receipt_form"] = form_state

            if not form_state or not form_state.get("ok"):
                return {
                    "ok": False,
                    "field": "post-receipt-form",
                    "debug": results,
                }

            time.sleep(0.25)
        else:
            results["receipt_validated"] = False

    # ------------------------------------------------------------
    # FIELDS AFTER RECEIPT
    # ------------------------------------------------------------

    # Merchant
    results["merchant"] = set_individual_text_field(
        "Merchant",
        item["merchant"],
    )
    if not results["merchant"] or not results["merchant"].get("ok"):
        return {
            "ok": False,
            "field": "merchant",
            "debug": results,
        }

    time.sleep(0.15)

    # Date - set directly as YYYY-MM-DD instead of using the calendar.
    # Receipt upload can re-render the datepicker and make calendar navigation
    # unreliable, while the underlying input accepts the ISO value directly.
    results["date"] = set_individual_date_direct(item["date"])
    if not results["date"] or not results["date"].get("ok"):
        return {
            "ok": False,
            "field": "date",
            "debug": results,
        }

    time.sleep(0.15)

    # Total
    results["total"] = set_individual_text_field(
        "Total",
        item["amount"],
    )
    if not results["total"] or not results["total"].get("ok"):
        return {
            "ok": False,
            "field": "total",
            "debug": results,
        }

    time.sleep(0.15)

    # Category
    results["category"] = select_individual_category(
        item["category"],
    )
    if not results["category"] or not results["category"].get("ok"):
        return {
            "ok": False,
            "field": "category",
            "debug": results,
        }

    time.sleep(0.15)

    # Description
    results["description"] = set_individual_text_field(
        "Description",
        item["description"],
    )
    if not results["description"] or not results["description"].get("ok"):
        return {
            "ok": False,
            "field": "description",
            "debug": results,
        }

    return {
        "ok": True,
        "strategy": "individual-expense-receipt-first",
        "is_per_diem": is_per_diem,
        "receipt_required": not is_per_diem,
        "receipt_validated": bool(results.get("receipt_validated")),
        "debug": results,
    }



def fill_batch(batch):
    """
    Fill Expensify's Multiple grid bottom-up.

    Why bottom-up:
    - Expensify shows 10 rows but only ~9 are visible at once.
    - Every untouched row has £0.00.
    - We select the LAST remaining £0.00 row, scroll it into view,
      fill that row, and set Total last.
    - Once Total becomes £15.00, that row drops out of the £0.00 set,
      so the next iteration naturally moves to the row above.

    This avoids relying on all 10 rows being simultaneously visible.
    """

    def js_json(js):
        raw = safari_js(js)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw}

    # ------------------------------------------------------------
    # Discover column X coordinates from the visible headers.
    # ------------------------------------------------------------
    geometry_js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0' &&
           r.width > 0 &&
           r.height > 0;
  };

  const norm = s => (s || '').replace(/\s+/g,' ').trim().toLowerCase();

  const dialog = [...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) {
    return JSON.stringify({ok:false, message:'No visible New Expense dialog'});
  }

  // Reset horizontal scroll inside likely grid/dialog containers before measuring.
  for (const el of dialog.querySelectorAll('.dialog_wrapper, .expenseDialog, .smallTabs_content, [class*="scroll"]')) {
    try { el.scrollLeft = 0; } catch (_) {}
  }

  const all = [...dialog.querySelectorAll('*')].filter(visible);

  const header = name => {
    const matches = all
      .filter(el => norm(el.innerText || el.textContent) === name)
      .map(el => {
        const r = el.getBoundingClientRect();
        return {
          el,
          left:r.left,
          right:r.right,
          top:r.top,
          bottom:r.bottom,
          width:r.width,
          height:r.height,
          cx:r.left + r.width/2,
          area:r.width*r.height,
          children:el.children.length
        };
      })
      .filter(x => x.top < 280)
      .sort((a,b) => {
        if (a.children !== b.children) return a.children - b.children;
        return a.area - b.area;
      });

    if (!matches.length) return null;
    const x = matches[0];
    return {
      left:Math.round(x.left),
      right:Math.round(x.right),
      cx:Math.round(x.cx),
      top:Math.round(x.top),
      bottom:Math.round(x.bottom)
    };
  };

  const headers = {
    date:header('date'),
    merchant:header('merchant'),
    total:header('total'),
    category:header('category'),
    description:header('description')
  };

  const totalZeros = [...dialog.querySelectorAll('*')]
    .filter(el => norm(el.innerText || el.textContent) === '£0.00')
    .length;

  return JSON.stringify({
    ok:!!(
      headers.date &&
      headers.merchant &&
      headers.total &&
      headers.category &&
      headers.description
    ),
    headers,
    totalZeroMatches:totalZeros
  });
})()
"""

    geometry = js_json(geometry_js)

    if not geometry or not geometry.get("ok"):
        return {
            "ok": False,
            "strategy": "physical-row-step",
            "message": "Could not identify Multiple grid headers",
            "geometry": geometry,
        }

    headers = geometry["headers"]



    def multiple_tab_is_active():
        """
        Return ok=True when the Multiple expense grid is usable.

        Expensify can temporarily remove LI.current after a receipt upload even
        though the Multiple grid is still on screen. So accept either:
          1) Multiple tab is explicitly current, OR
          2) the Multiple grid itself is visibly present.
        """
        js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const norm=s => (s || '').replace(/\s+/g,' ').trim().toLowerCase();

  const dialog=[...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) {
    return JSON.stringify({
      ok:false,
      reason:'no-visible-new-expense-dialog',
      current:[]
    });
  }

  const current=[...dialog.querySelectorAll('li.current')]
    .filter(visible)
    .map(el => norm(el.innerText || el.textContent));

  const multipleCurrent=current.includes('multiple');

  const txt=norm(dialog.innerText || dialog.textContent);

  const hasHeaders =
    txt.includes('date') &&
    txt.includes('merchant') &&
    txt.includes('total') &&
    txt.includes('category') &&
    txt.includes('description');

  const zeroRows=(txt.match(/£0\.00/g) || []).length;
  const hasGrid=hasHeaders && zeroRows >= 1;

  return JSON.stringify({
    ok:multipleCurrent || hasGrid,
    multipleCurrent,
    hasGrid,
    zeroRows,
    current,
    dialogText:(dialog.innerText || '').replace(/\s+/g,' ').slice(0,900)
  });
})()
"""
        return js_json(js)

    def wait_for_multiple_grid(timeout_seconds=6.0):
        """Wait for the Multiple grid to become usable after receipt upload."""
        deadline = time.time() + timeout_seconds
        last = None

        while time.time() < deadline:
            last = multiple_tab_is_active()
            if last and last.get("ok"):
                return last
            time.sleep(0.20)

        return last or {
            "ok": False,
            "reason": "timeout-waiting-for-multiple-grid",
        }


    def refresh_headers():
        """Re-read visible column coordinates because Expensify can move the dialog."""
        js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0' &&
           r.width > 0 &&
           r.height > 0;
  };

  const norm = s => (s || '').replace(/\s+/g,' ').trim().toLowerCase();

  const dialog = [...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) {
    return JSON.stringify({ok:false, message:'No visible New Expense dialog'});
  }

  const all = [...dialog.querySelectorAll('*')].filter(visible);

  const header = name => {
    const matches = all
      .filter(el => norm(el.innerText || el.textContent) === name)
      .map(el => {
        const r = el.getBoundingClientRect();
        return {
          el,
          left:r.left,
          right:r.right,
          top:r.top,
          bottom:r.bottom,
          width:r.width,
          height:r.height,
          cx:r.left+r.width/2,
          area:r.width*r.height,
          children:el.children.length
        };
      })
      .sort((a,b) => {
        if (a.children !== b.children) return a.children-b.children;
        return a.area-b.area;
      });

    if (!matches.length) return null;
    const x=matches[0];

    return {
      left:Math.round(x.left),
      right:Math.round(x.right),
      cx:Math.round(x.cx),
      top:Math.round(x.top),
      bottom:Math.round(x.bottom)
    };
  };

  const headers = {
    date:header('date'),
    merchant:header('merchant'),
    total:header('total'),
    category:header('category'),
    description:header('description')
  };

  const dr=dialog.getBoundingClientRect();

  return JSON.stringify({
    ok:!!(
      headers.date &&
      headers.merchant &&
      headers.total &&
      headers.category &&
      headers.description
    ),
    headers,
    dialog:{
      left:Math.round(dr.left),
      right:Math.round(dr.right),
      top:Math.round(dr.top),
      bottom:Math.round(dr.bottom)
    }
  });
})()
"""
        return js_json(js)



    def get_grid_rows_bottom_up():
        """
        Return distinct Multiple-grid row centers, ordered bottom -> top.

        This is intentionally independent of whether the row still contains
        £0.00. Once a CSV row is assigned to a grid row, the next CSV row uses
        the next physical row above it.
        """
        js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const dialog=[...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) {
    return JSON.stringify({
      ok:false,
      message:'No visible New Expense dialog'
    });
  }

  // Expensify's physical expense rows expose these TD classes.
  const cells=[...dialog.querySelectorAll(
    'td.datecolumn, td.amountcolumn, td.merchantcolumn, td.categorycolumn, td.commentcolumn'
  )]
    .filter(visible)
    .map(el => {
      const r=el.getBoundingClientRect();
      return {
        tag:el.tagName,
        cls:String(el.className || ''),
        top:r.top,
        bottom:r.bottom,
        cy:r.top+r.height/2,
        left:r.left,
        width:r.width,
        height:r.height
      };
    });

  const ys=[];

  for (const c of cells) {
    if (!ys.some(y => Math.abs(y-c.cy) < 8)) {
      ys.push(c.cy);
    }
  }

  ys.sort((a,b) => b-a);

  return JSON.stringify({
    ok:ys.length>0,
    rowYs:ys.map(y => Math.round(y)),
    count:ys.length
  });
})()
"""
        return js_json(js)

    def row_y_for_position_from_bottom(position_from_bottom):
        """
        Get the Y coordinate for a physical grid row.

        position_from_bottom:
          0 = bottom row
          1 = one row above bottom
          2 = two rows above bottom
          ...
        """
        rows = get_grid_rows_bottom_up()

        if not rows or not rows.get("ok"):
            return {
                "ok": False,
                "message": "Could not detect Multiple grid rows",
                "rows": rows,
            }

        row_ys = rows.get("rowYs", [])

        if position_from_bottom >= len(row_ys):
            return {
                "ok": False,
                "message": (
                    f"Requested row {position_from_bottom + 1} from bottom, "
                    f"but only {len(row_ys)} physical rows are visible"
                ),
                "rows": rows,
            }

        return {
            "ok": True,
            "rowY": row_ys[position_from_bottom],
            "positionFromBottom": position_from_bottom,
            "rows": rows,
        }


    def get_last_zero_row():
        """
        Find the bottom-most remaining £0.00 row, scroll it into view,
        and return its new viewport Y coordinate.
        """
        js = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g,' ').trim();

  const cssVisible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0';
  };

  const dialog = [...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(cssVisible)[0];

  if (!dialog) {
    return JSON.stringify({ok:false, message:'No New Expense dialog'});
  }

  // Exact £0.00 elements, including ones below the currently visible viewport.
  let zeros = [...dialog.querySelectorAll('*')]
    .filter(cssVisible)
    .filter(el => norm(el.innerText || el.textContent) === '£0.00')
    .map(el => {
      const r = el.getBoundingClientRect();
      return {
        el,
        top:r.top,
        left:r.left,
        width:r.width,
        height:r.height,
        area:r.width*r.height
      };
    });

  if (!zeros.length) {
    return JSON.stringify({ok:false, message:'No remaining £0.00 row found'});
  }

  // Deduplicate nested exact-text matches by choosing small leaf-like elements.
  zeros.sort((a,b) => {
    const ac = a.el.children.length;
    const bc = b.el.children.length;
    if (ac !== bc) return ac-bc;
    if (a.top !== b.top) return b.top-a.top;
    return a.area-b.area;
  });

  const unique = [];
  for (const z of zeros) {
    if (!unique.some(u => Math.abs(u.top-z.top) < 6)) {
      unique.push(z);
    }
  }

  // Choose the visually bottom-most remaining zero row.
  unique.sort((a,b) => b.top-a.top);
  const chosen = unique[0];

  chosen.el.scrollIntoView({
    block:'center',
    inline:'nearest'
  });

  const r2 = chosen.el.getBoundingClientRect();

  return JSON.stringify({
    ok:true,
    zeroCount:unique.length,
    rowY:Math.round(r2.top+r2.height/2),
    zero:{
      tag:chosen.el.tagName,
      cls:String(chosen.el.className || '').slice(0,160),
      text:norm(chosen.el.innerText || chosen.el.textContent),
      left:Math.round(r2.left),
      top:Math.round(r2.top),
      width:Math.round(r2.width),
      height:Math.round(r2.height)
    }
  });
})()
"""
        return js_json(js)

    def click_cell(row_y, x, field_name):
        field_json = json.dumps(field_name)
        js = f"""
(() => {{
  const rowY = {int(row_y)};
  const targetX = {int(x)};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0' &&
           r.width > 0 &&
           r.height > 0;
  }};

  const dialog = [...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) return JSON.stringify({{ok:false, message:'No dialog'}});

  const dr = dialog.getBoundingClientRect();

  const candidates = [...dialog.querySelectorAll('td, div, span, a')]
    .filter(visible)
    .map(el => {{
      const r = el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        right:r.right,
        top:r.top,
        width:r.width,
        height:r.height,
        cx:r.left+r.width/2,
        cy:r.top+r.height/2,
        area:r.width*r.height,
        text:(el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim()
      }};
    }})
    .filter(c =>
      Math.abs(c.cy-rowY) < 30 &&
      c.height >= 18 &&
      c.height <= 90 &&
      c.width >= 25 &&
      c.left >= dr.left - 5 &&
      c.right <= dr.right + 5
    )
    .map(c => ({{
      ...c,
      containsX:c.left <= targetX && c.right >= targetX,
      dist:Math.abs(c.cx-targetX)
    }}))
    .sort((a,b) => {{
      if (a.containsX !== b.containsX) return a.containsX ? -1 : 1;
      if (a.dist !== b.dist) return a.dist-b.dist;
      return a.area-b.area;
    }});

  if (!candidates.length) {{
    return JSON.stringify({{
      ok:false,
      field:{field_json},
      message:'No cell candidate',
      rowY,
      targetX
    }});
  }}

  const hit = candidates[0];

  try {{
    hit.el.scrollIntoView({{block:'nearest', inline:'nearest'}});
    hit.el.click();
  }} catch (e) {{
    return JSON.stringify({{
      ok:false,
      field:{field_json},
      message:String(e)
    }});
  }}

  return JSON.stringify({{
    ok:true,
    field:{field_json},
    targetX,
    dialogBounds:{{
      left:Math.round(dr.left),
      right:Math.round(dr.right),
      top:Math.round(dr.top),
      bottom:Math.round(dr.bottom)
    }},
    chosen:{{
      tag:hit.el.tagName,
      cls:String(hit.el.className || '').slice(0,140),
      text:hit.text.slice(0,160),
      left:Math.round(hit.left),
      top:Math.round(hit.top),
      width:Math.round(hit.width),
      height:Math.round(hit.height),
      containsX:hit.containsX,
      dist:Math.round(hit.dist)
    }}
  }});
}})()
"""
        return js_json(js)

    def set_nearest_editor(row_y, x, field_name, value):
        field_json = json.dumps(field_name)
        value_json = json.dumps(value)

        js = f"""
(() => {{
  const rowY = {int(row_y)};
  const targetX = {int(x)};
  const value = {value_json};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0' &&
           r.width > 0 &&
           r.height > 0;
  }};

  const dialog = [...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) return JSON.stringify({{ok:false, message:'No dialog'}});

  const editors = [...dialog.querySelectorAll(
    'input:not([type="hidden"]), textarea, [contenteditable="true"]'
  )]
    .filter(visible)
    .map(el => {{
      const r = el.getBoundingClientRect();
      return {{
        el,
        cx:r.left+r.width/2,
        cy:r.top+r.height/2,
        left:r.left,
        top:r.top,
        width:r.width,
        height:r.height,
        dx:Math.abs((r.left+r.width/2)-targetX),
        dy:Math.abs((r.top+r.height/2)-rowY)
      }};
    }})
    .filter(x => x.dy < 60)
    .sort((a,b) => (a.dy*5+a.dx)-(b.dy*5+b.dx));

  if (!editors.length) {{
    return JSON.stringify({{
      ok:false,
      field:{field_json},
      message:'No editor appeared near clicked cell',
      visibleEditors:[...dialog.querySelectorAll(
        'input:not([type="hidden"]), textarea, [contenteditable="true"]'
      )].filter(visible).map(el => {{
        const r=el.getBoundingClientRect();
        return {{
          tag:el.tagName,
          type:el.getAttribute('type') || '',
          name:el.getAttribute('name') || '',
          cls:String(el.className || '').slice(0,120),
          value:el.value || el.innerText || '',
          left:Math.round(r.left),
          top:Math.round(r.top),
          width:Math.round(r.width),
          height:Math.round(r.height)
        }};
      }})
    }});
  }}

  const editor = editors[0].el;

  if (editor.matches('[contenteditable="true"]')) {{
    editor.focus();
    editor.innerText = value;
  }} else {{
    const proto = editor.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;

    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    editor.focus();
    if (setter) setter.call(editor, value);
    else editor.value = value;
  }}

  editor.dispatchEvent(new Event('input', {{bubbles:true}}));
  editor.dispatchEvent(new Event('change', {{bubbles:true}}));
  editor.blur();

  const r=editor.getBoundingClientRect();

  return JSON.stringify({{
    ok:true,
    field:{field_json},
    requested:value,
    editor:{{
      tag:editor.tagName,
      type:editor.getAttribute('type') || '',
      name:editor.getAttribute('name') || '',
      cls:String(editor.className || '').slice(0,140),
      value:editor.value || editor.innerText || '',
      left:Math.round(r.left),
      top:Math.round(r.top),
      width:Math.round(r.width),
      height:Math.round(r.height)
    }}
  }});
}})()
"""
        return js_json(js)

    def select_date_from_calendar(target_date):
        """
        Select a date from Expensify's popup calendar.

        Important: Expensify also renders a small element with class
        `expensicons-calendar`. That is only the icon, not the date picker.
        We identify the real calendar by its rendered text: month + year +
        weekday/day grid.
        """
        target_year = target_date.year
        target_month = target_date.month
        target_day = target_date.day

        month_names = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december"
        ]

        def read_calendar():
            js = f"""
(() => {{
  const months = {json.dumps(month_names)};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0' &&
           r.width > 120 &&
           r.height > 100;
  }};

  const norm = s => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();

  const candidates = [...document.querySelectorAll('body *')]
    .filter(visible)
    .map(el => {{
      const r = el.getBoundingClientRect();
      const txt = norm(el.innerText || el.textContent);
      const monthIndex = months.findIndex(m => txt.includes(m));
      const yearMatch = txt.match(/20\\d{{2}}/);

      // A real calendar should contain a month/year and several weekday labels.
      const weekdayHits = ['su','mo','tu','we','th','fr','sa']
        .filter(d => new RegExp(`(^|\\\\s)${{d}}(\\\\s|$)`).test(txt))
        .length;

      return {{
        el,
        txt,
        monthIndex,
        year:yearMatch ? parseInt(yearMatch[0],10) : null,
        weekdayHits,
        area:r.width*r.height,
        left:r.left,
        top:r.top,
        width:r.width,
        height:r.height
      }};
    }})
    .filter(x =>
      x.monthIndex >= 0 &&
      x.year !== null &&
      x.weekdayHits >= 4
    )
    .sort((a,b) => a.area-b.area);

  if (!candidates.length) {{
    return JSON.stringify({{
      ok:false,
      message:'No real calendar popup found',
      likely:[...document.querySelectorAll('body *')]
        .filter(el => {{
          const s=getComputedStyle(el), r=el.getBoundingClientRect();
          if (
            s.display==='none' ||
            s.visibility==='hidden' ||
            r.width < 100 ||
            r.height < 60
          ) return false;

          const t=norm(el.innerText || el.textContent);
          return months.some(m => t.includes(m)) && /20\\d{{2}}/.test(t);
        }})
        .slice(0,20)
        .map(el => {{
          const r=el.getBoundingClientRect();
          return {{
            tag:el.tagName,
            cls:String(el.className || '').slice(0,160),
            text:norm(el.innerText || el.textContent).slice(0,700),
            left:Math.round(r.left),
            top:Math.round(r.top),
            width:Math.round(r.width),
            height:Math.round(r.height)
          }};
        }})
    }});
  }}

  const c=candidates[0];

  return JSON.stringify({{
    ok:true,
    monthIndex:c.monthIndex,
    month:c.monthIndex+1,
    year:c.year,
    text:c.txt.slice(0,900),
    tag:c.el.tagName,
    cls:String(c.el.className || '').slice(0,180),
    left:Math.round(c.left),
    top:Math.round(c.top),
    width:Math.round(c.width),
    height:Math.round(c.height)
  }});
}})()
"""
            return js_json(js)

        # Wait for actual popup, not icon.
        calendar = None
        for _ in range(25):
            calendar = read_calendar()
            if calendar and calendar.get("ok"):
                break
            time.sleep(0.10)

        if not calendar or not calendar.get("ok"):
            return {
                "ok": False,
                "message": "Date cell clicked but real calendar popup was not detected",
                "calendar": calendar,
            }

        # Navigate month-by-month.
        for _ in range(36):
            calendar = read_calendar()

            if not calendar or not calendar.get("ok"):
                return {
                    "ok": False,
                    "message": "Calendar disappeared while navigating",
                    "calendar": calendar,
                }

            cur_year = int(calendar["year"])
            cur_month = int(calendar["month"])

            if cur_year == target_year and cur_month == target_month:
                break

            current_abs = cur_year * 12 + cur_month
            target_abs = target_year * 12 + target_month
            direction = "prev" if current_abs > target_abs else "next"

            nav_js = f"""
(() => {{
  const months = {json.dumps(month_names)};
  const direction = {json.dumps(direction)};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  }};

  const norm = s => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();

  // Find the real calendar again by month/year + weekday grid.
  const containers=[...document.querySelectorAll('body *')]
    .filter(el => {{
      if (!visible(el)) return false;
      const r=el.getBoundingClientRect();
      if (r.width < 120 || r.height < 100) return false;

      const txt=norm(el.innerText || el.textContent);
      const hasMonth=months.some(m => txt.includes(m));
      const hasYear=/20\\d{{2}}/.test(txt);
      const weekdayHits=['su','mo','tu','we','th','fr','sa']
        .filter(d => new RegExp(`(^|\\\\s)${{d}}(\\\\s|$)`).test(txt))
        .length;

      return hasMonth && hasYear && weekdayHits >= 4;
    }})
    .sort((a,b) => {{
      const ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
      return (ra.width*ra.height)-(rb.width*rb.height);
    }});

  if (!containers.length) {{
    return JSON.stringify({{
      ok:false,
      message:'Calendar container not found'
    }});
  }}

  const cal=containers[0];
  const cr=cal.getBoundingClientRect();

  const clickableAncestor = el => {{
    let cur=el;
    for (let i=0; cur && i<5; i++,cur=cur.parentElement) {{
      const role=norm(cur.getAttribute?.('role'));
      const cls=norm(String(cur.className || ''));
      const style=getComputedStyle(cur);
      if (
        cur.tagName==='A' ||
        cur.tagName==='BUTTON' ||
        role==='button' ||
        cur.hasAttribute?.('tabindex') ||
        style.cursor==='pointer' ||
        cls.includes('button') ||
        cls.includes('arrow') ||
        cls.includes('nav')
      ) return cur;
    }}
    return el;
  }};

  // Restrict to small elements in the top part of the calendar.
  let headerBits=[...cal.querySelectorAll('a,button,span,div,i')]
    .filter(visible)
    .map(el => {{
      const r=el.getBoundingClientRect();
      return {{
        el,
        left:r.left,
        top:r.top,
        right:r.right,
        width:r.width,
        height:r.height,
        cx:r.left+r.width/2,
        cy:r.top+r.height/2,
        text:norm(
          el.getAttribute('title') || ''
        ) + ' ' + norm(
          el.getAttribute('aria-label') || ''
        ) + ' ' + norm(
          el.innerText || el.textContent
        ) + ' ' + norm(
          String(el.className || '')
        )
      }};
    }})
    .filter(x =>
      x.top >= cr.top - 5 &&
      x.top <= cr.top + Math.min(90, cr.height*0.30) &&
      x.width > 5 &&
      x.height > 5 &&
      x.width <= 90 &&
      x.height <= 70
    );

  // First try semantic matches.
  let choice=headerBits.find(x => {{
    const t=x.text;
    return direction==='prev'
      ? (t.includes('previous') || t.includes('prev') || t.includes('left'))
      : (t.includes('next') || t.includes('right'));
  }});

  // Geometric fallback: left-most or right-most small control in calendar header.
  if (!choice && headerBits.length) {{
    headerBits.sort((a,b) => a.cx-b.cx);
    choice = direction==='prev'
      ? headerBits[0]
      : headerBits[headerBits.length-1];
  }}

  if (!choice) {{
    return JSON.stringify({{
      ok:false,
      message:'No calendar header navigation candidate',
      direction,
      calendarRect:{{
        left:Math.round(cr.left),
        top:Math.round(cr.top),
        width:Math.round(cr.width),
        height:Math.round(cr.height)
      }}
    }});
  }}

  const target=clickableAncestor(choice.el);
  const tr=target.getBoundingClientRect();

  // Activate exactly once.
  try {{
    target.click();
  }} catch (_) {{
    target.dispatchEvent(new MouseEvent('click',{{
      bubbles:true,
      cancelable:true,
      view:window,
      clientX:tr.left+tr.width/2,
      clientY:tr.top+tr.height/2,
      button:0
    }}));
  }}

  return JSON.stringify({{
    ok:true,
    direction,
    candidate:{{
      tag:choice.el.tagName,
      cls:String(choice.el.className || '').slice(0,160),
      text:choice.text.slice(0,220),
      left:Math.round(choice.left),
      top:Math.round(choice.top),
      width:Math.round(choice.width),
      height:Math.round(choice.height)
    }},
    target:{{
      tag:target.tagName,
      cls:String(target.className || '').slice(0,160),
      role:target.getAttribute?.('role') || '',
      left:Math.round(tr.left),
      top:Math.round(tr.top),
      width:Math.round(tr.width),
      height:Math.round(tr.height)
    }}
  }});
}})()
"""
            nav_result = js_json(nav_js)

            if not nav_result or not nav_result.get("ok"):
                return {
                    "ok": False,
                    "message": f"Could not navigate calendar {direction}",
                    "detail": nav_result,
                    "calendar": calendar,
                }

            # Wait briefly and verify the displayed month/year actually changed.
            before_pair = (cur_year, cur_month)
            changed = False
            verify = None

            for _verify in range(12):
                time.sleep(0.08)
                verify = read_calendar()
                if verify and verify.get("ok"):
                    after_pair = (int(verify["year"]), int(verify["month"]))
                    if after_pair != before_pair:
                        changed = True
                        break

            if not changed:
                return {
                    "ok": False,
                    "message": "Calendar navigation control was clicked but month did not change",
                    "direction": direction,
                    "before": {
                        "year": cur_year,
                        "month": cur_month,
                    },
                    "click": nav_result,
                    "after": verify,
                }
        else:
            return {
                "ok": False,
                "message": "Calendar navigation exceeded safety limit",
            }

        # Click target day in current month.
        day_js = f"""
(() => {{
  const months = {json.dumps(month_names)};
  const wantedDay = {int(target_day)};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           r.width>0 &&
           r.height>0;
  }};

  const norm = s => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();

  const containers=[...document.querySelectorAll('body *')]
    .filter(el => {{
      if (!visible(el)) return false;
      const r=el.getBoundingClientRect();
      if (r.width < 120 || r.height < 100) return false;

      const txt=norm(el.innerText || el.textContent);
      const hasMonth=months.some(m => txt.includes(m));
      const hasYear=/20\\d{{2}}/.test(txt);
      const weekdayHits=['su','mo','tu','we','th','fr','sa']
        .filter(d => new RegExp(`(^|\\\\s)${{d}}(\\\\s|$)`).test(txt))
        .length;

      return hasMonth && hasYear && weekdayHits >= 4;
    }})
    .sort((a,b) => {{
      const ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
      return (ra.width*ra.height)-(rb.width*rb.height);
    }});

  if (!containers.length) {{
    return JSON.stringify({{
      ok:false,
      message:'Calendar container not found for day selection'
    }});
  }}

  const cal=containers[0];

  let days=[...cal.querySelectorAll('a, button, td, span, div')]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent) === String(wantedDay))
    .map(el => {{
      const r=el.getBoundingClientRect();
      const cls=String(el.className || '').toLowerCase();
      const parentCls=String(el.parentElement?.className || '').toLowerCase();

      return {{
        el,
        area:r.width*r.height,
        cls,
        parentCls,
        tag:el.tagName
      }};
    }})
    .filter(x =>
      !x.cls.includes('other') &&
      !x.parentCls.includes('other') &&
      !x.cls.includes('disabled') &&
      !x.parentCls.includes('disabled')
    )
    .sort((a,b) => {{
      const aClick = ['A','BUTTON'].includes(a.tag) ? 0 : 10;
      const bClick = ['A','BUTTON'].includes(b.tag) ? 0 : 10;
      if (aClick !== bClick) return aClick-bClick;
      return a.area-b.area;
    }});

  if (!days.length) {{
    return JSON.stringify({{
      ok:false,
      message:'Requested day not found',
      wantedDay,
      calendar:(cal.innerText || '').replace(/\\s+/g,' ').slice(0,800)
    }});
  }}

  const target=days[0].el;
  target.click();

  return JSON.stringify({{
    ok:true,
    day:wantedDay,
    tag:target.tagName,
    cls:String(target.className || '').slice(0,140)
  }});
}})()
"""
        day_result = js_json(day_js)

        if not day_result or not day_result.get("ok"):
            return {
                "ok": False,
                "message": "Could not click requested day",
                "detail": day_result,
                "calendar": read_calendar(),
            }

        time.sleep(0.15)

        return {
            "ok": True,
            "date": target_date.strftime("%d/%m/%Y"),
            "day_result": day_result,
        }



    def choose_dropdown_option(option_text, row_y, column_x):
        """
        Select an autocomplete/dropdown option for Category.

        Critical safety rule:
        Never click the top New Expense tabs (Expense / Distance / Per Diem /
        Multiple). The Category dropdown may contain the same text "Per Diem",
        so selection is scoped geometrically near the Category cell/editor and
        explicitly excludes .dialogTabs / .bigTabs.
        """
        option_json = json.dumps(option_text)

        js = f"""
(() => {{
  const wanted = {option_json}.trim().toLowerCase();
  const rowY = {int(row_y)};
  const columnX = {int(column_x)};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' &&
           s.visibility !== 'hidden' &&
           s.opacity !== '0' &&
           r.width > 0 &&
           r.height > 0;
  }};

  const norm = s => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();

  const multipleActive = [...document.querySelectorAll(
    '.dialog.dialog_new.NewExpense li.current'
  )].some(el => norm(el.innerText || el.textContent) === 'multiple');

  if (!multipleActive) {{
    return JSON.stringify({{
      ok:false,
      message:'Multiple tab is not active before Category option selection'
    }});
  }}

  const raw = [...document.querySelectorAll(
    'a, li, div, span, [role="option"], [role="menuitem"]'
  )]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent) === wanted)
    .filter(el => !el.closest('.dialogTabs'))
    .filter(el => !el.closest('.bigTabs'))
    .filter(el => !el.closest('li.current'))
    .map(el => {{
      const r = el.getBoundingClientRect();
      const cls = String(el.className || '');
      const parentCls = String(el.parentElement?.className || '');
      const role = el.getAttribute('role') || '';

      return {{
        el,
        tag:el.tagName,
        cls,
        parentCls,
        role,
        left:r.left,
        right:r.right,
        top:r.top,
        bottom:r.bottom,
        width:r.width,
        height:r.height,
        cx:r.left+r.width/2,
        cy:r.top+r.height/2,
        area:r.width*r.height,
        dx:Math.abs((r.left+r.width/2)-columnX),
        dy:Math.abs((r.top+r.height/2)-rowY)
      }};
    }})
    // Dropdown should be reasonably near the Category column/row.
    .filter(x => x.dx < 300 && x.dy < 300)
    .map(x => {{
      const menuish = /option|menu|autocomplete|suggest|result|select|dropdown/i.test(
        x.cls + ' ' + x.parentCls + ' ' + x.role
      ) ? 0 : 100;

      return {{
        ...x,
        score:menuish + x.dx + x.dy*0.7 + Math.min(x.area/5000, 50)
      }};
    }})
    .sort((a,b) => a.score-b.score);

  if (!raw.length) {{
    return JSON.stringify({{
      ok:false,
      message:'No scoped Category dropdown option found',
      wanted:{option_json},
      rowY,
      columnX,
      exactMatches:[...document.querySelectorAll('a,li,div,span')]
        .filter(visible)
        .filter(el => norm(el.innerText || el.textContent) === wanted)
        .slice(0,30)
        .map(el => {{
          const r=el.getBoundingClientRect();
          return {{
            tag:el.tagName,
            cls:String(el.className || '').slice(0,140),
            parentCls:String(el.parentElement?.className || '').slice(0,140),
            inDialogTabs:!!el.closest('.dialogTabs'),
            inBigTabs:!!el.closest('.bigTabs'),
            left:Math.round(r.left),
            top:Math.round(r.top),
            width:Math.round(r.width),
            height:Math.round(r.height)
          }};
        }})
    }});
  }}

  const target = raw[0].el;

  try {{
    target.click();
  }} catch (e) {{
    return JSON.stringify({{
      ok:false,
      message:String(e),
      wanted:{option_json}
    }});
  }}

  const stillMultiple = [...document.querySelectorAll(
    '.dialog.dialog_new.NewExpense li.current'
  )].some(el => norm(el.innerText || el.textContent) === 'multiple');

  if (!stillMultiple) {{
    return JSON.stringify({{
      ok:false,
      message:'Category option click unexpectedly changed away from Multiple tab',
      chosen:{{
        tag:target.tagName,
        cls:String(target.className || '').slice(0,160),
        parentCls:String(target.parentElement?.className || '').slice(0,160)
      }}
    }});
  }}

  const r=target.getBoundingClientRect();

  return JSON.stringify({{
    ok:true,
    wanted:{option_json},
    tag:target.tagName,
    cls:String(target.className || '').slice(0,160),
    parentCls:String(target.parentElement?.className || '').slice(0,160),
    role:target.getAttribute('role') || '',
    left:Math.round(r.left),
    top:Math.round(r.top),
    width:Math.round(r.width),
    height:Math.round(r.height),
    multipleStillActive:stillMultiple
  }});
}})()
"""
        return js_json(js)



    def click_attachment_button(row_y):
        """Click the attachment/receipt button between currency and Category."""
        fresh = refresh_headers()
        if not fresh or not fresh.get("ok"):
            return {
                "ok": False,
                "message": "Could not refresh headers before attachment",
                "fresh": fresh,
            }

        h = fresh["headers"]
        left_bound = h["total"]["right"]
        right_bound = h["category"]["left"]

        js = f"""
(() => {{
  const rowY = {int(row_y)};
  const leftBound = {int(left_bound)};
  const rightBound = {int(right_bound)};

  const visible = el => {{
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  }};

  const norm = s => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();

  const dialog=[...document.querySelectorAll('.dialog.dialog_new.NewExpense')]
    .filter(visible)[0];

  if (!dialog) return JSON.stringify({{ok:false,message:'No dialog'}});

  const items=[...dialog.querySelectorAll(
    'button, a, [role="button"], span, div, i'
  )]
    .filter(visible)
    .map(el => {{
      const r=el.getBoundingClientRect();
      const semantic=norm(
        (el.getAttribute('title') || '') + ' ' +
        (el.getAttribute('aria-label') || '') + ' ' +
        String(el.className || '') + ' ' +
        (el.innerText || el.textContent || '')
      );

      const clickable =
        el.tagName==='BUTTON' ||
        el.tagName==='A' ||
        norm(el.getAttribute('role'))==='button' ||
        getComputedStyle(el).cursor==='pointer' ||
        typeof el.onclick==='function' ||
        /button|attach|receipt|upload|paperclip|camera/.test(
          String(el.className || '').toLowerCase()
        );

      return {{
        el,
        semantic,
        clickable,
        left:r.left,
        right:r.right,
        top:r.top,
        width:r.width,
        height:r.height,
        cx:r.left+r.width/2,
        cy:r.top+r.height/2,
        area:r.width*r.height
      }};
    }})
    .filter(x =>
      x.clickable &&
      Math.abs(x.cy-rowY) < 32 &&
      x.cx > leftBound &&
      x.cx < rightBound &&
      x.width >= 8 &&
      x.height >= 8 &&
      x.width <= 90 &&
      x.height <= 70
    )
    .map(x => {{
      const semanticBonus =
        /attach|receipt|upload|paperclip|camera/.test(x.semantic) ? -1000 : 0;
      const center=(leftBound+rightBound)/2;
      return {{
        ...x,
        score:semanticBonus + Math.abs(x.cx-center) + x.area/5000
      }};
    }})
    .sort((a,b) => a.score-b.score);

  if (!items.length) {{
    return JSON.stringify({{
      ok:false,
      message:'No attachment control found between currency and Category',
      rowY,
      leftBound,
      rightBound
    }});
  }}

  const hit=items[0];

  try {{
    hit.el.click();
  }} catch (e) {{
    return JSON.stringify({{ok:false,message:String(e)}});
  }}

  return JSON.stringify({{
    ok:true,
    chosen:{{
      tag:hit.el.tagName,
      cls:String(hit.el.className || '').slice(0,180),
      title:hit.el.getAttribute('title') || '',
      aria:hit.el.getAttribute('aria-label') || '',
      text:(hit.el.innerText || hit.el.textContent || '').trim().slice(0,120),
      left:Math.round(hit.left),
      top:Math.round(hit.top),
      width:Math.round(hit.width),
      height:Math.round(hit.height)
    }},
    bounds:{{left:leftBound,right:rightBound}}
  }});
}})()
"""
        return js_json(js)

    def click_import_from_computer():
        """Click the exact visible Import From Computer control."""
        js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const norm=s => (s || '').replace(/\s+/g,' ').trim().toLowerCase();

  const matches=[...document.querySelectorAll(
    'button, a, div, [role="button"]'
  )]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)==='import from computer')
    .map(el => {
      const r=el.getBoundingClientRect();
      const cls=String(el.className || '');
      const green=/success|green|primary/.test(cls.toLowerCase()) ? 0 : 50;
      return {
        el,
        cls,
        green,
        area:r.width*r.height,
        left:r.left,
        top:r.top,
        width:r.width,
        height:r.height
      };
    })
    .sort((a,b) => {
      if (a.green!==b.green) return a.green-b.green;
      return a.area-b.area;
    });

  if (!matches.length) {
    return JSON.stringify({
      ok:false,
      message:'Visible Import From Computer button not found'
    });
  }

  const hit=matches[0];
  hit.el.click();

  return JSON.stringify({
    ok:true,
    tag:hit.el.tagName,
    cls:hit.cls.slice(0,180),
    left:Math.round(hit.left),
    top:Math.round(hit.top),
    width:Math.round(hit.width),
    height:Math.round(hit.height)
  });
})()
"""
        last = None
        for _ in range(20):
            last = js_json(js)
            if last and last.get("ok"):
                return last
            time.sleep(0.10)

        return last or {
            "ok": False,
            "message": "Import From Computer button did not appear",
        }

    def choose_file_in_macos_dialog(file_path):
        """Choose a local file in Safari's native macOS Open dialog."""
        p = str(Path(file_path).expanduser().resolve())

        cp = subprocess.run(
            ["pbcopy"],
            input=p,
            text=True,
            capture_output=True,
        )
        if cp.returncode != 0:
            return {
                "ok": False,
                "message": "Could not copy attachment path to clipboard",
            }

        applescript = """
tell application "System Events"
    tell process "Safari"
        set frontmost to true
        delay 0.6
        keystroke "g" using {command down, shift down}
        delay 0.4
        keystroke "v" using {command down}
        delay 0.3
        key code 36
        delay 0.8
        key code 36
        delay 1.2
    end tell
end tell
"""

        try:
            run_osascript(applescript)
        except Exception as e:
            return {
                "ok": False,
                "message": f"macOS file picker automation failed: {e}",
                "path": p,
            }

        return {"ok": True, "path": p}

    def attach_file_to_row(row_y, file_path):
        """Attach one local file to the current Multiple-expense row."""
        result = {"path": str(Path(file_path).expanduser())}

        click_result = click_attachment_button(row_y)
        result["attachment_button"] = click_result
        if not click_result or not click_result.get("ok"):
            result["ok"] = False
            return result

        time.sleep(0.25)

        import_result = click_import_from_computer()
        result["import_from_computer"] = import_result
        if not import_result or not import_result.get("ok"):
            result["ok"] = False
            return result

        time.sleep(0.45)

        picker_result = choose_file_in_macos_dialog(file_path)
        result["file_picker"] = picker_result
        if not picker_result or not picker_result.get("ok"):
            result["ok"] = False
            return result

        time.sleep(1.0)
        result["ok"] = True
        return result


    results = []

    # IMPORTANT: work bottom-up so the last remaining £0.00 always identifies
    # the row we are currently filling.
    # Work through the CSV batch from its last item to its first, but assign
    # each item to a DIFFERENT physical row. Step 0 = bottom row, step 1 =
    # one row above, etc. This prevents a later CSV entry from overwriting
    # the row that was just filled.
    for step, i in enumerate(range(len(batch) - 1, -1, -1)):
        item = batch[i]

        if isinstance(item, dict):
            d = item["date"]
            merchant_value = item["merchant"]
            amount_value = item["amount"]
            category_value = item["category"]
            description_value = item["description"]
            attachment_value = item.get("receipt_path")
            currency_value = item.get("currency", "GBP")
        else:
            d = item
            merchant_value = MERCHANT
            amount_value = AMOUNT
            category_value = "Per Diem"
            description_value = DESCRIPTION
            attachment_value = ATTACHMENT_FILE
            currency_value = "GBP"

        anchor = row_y_for_position_from_bottom(step)
        if not anchor or not anchor.get("ok"):
            return {
                "ok": False,
                "strategy": "physical-row-step",
                "message": (
                    f"Could not locate physical grid row {step + 1} "
                    f"from bottom for CSV expense {i+1}"
                ),
                "anchor": anchor,
                "results": results,
            }

        row_y = anchor["rowY"]

        row_result = {
            "row": i + 1,
            "date": d.strftime("%d/%m/%Y"),
            "merchant": merchant_value,
            "total": amount_value,
            "currency": currency_value,
            "category": category_value,
            "description": description_value,
            "attachment": attachment_value,
            "physical_row_from_bottom": step + 1,
            "anchor": anchor,
            "debug": {},
        }

        # Total MUST be last because changing £0.00 -> £15.00 removes
        # this row from our anchor set.
        #
        # Do NOT cache X coordinates here. Expensify can move the entire
        # dialog after the Date calendar closes. We refresh the headers
        # immediately before every field.
        fields = [
            ("date", d.strftime("%d/%m/%Y")),
            ("merchant", merchant_value),
            ("description", description_value),
            ("category", category_value),
        ]

        if attachment_value:
            fields.append(("attachment", attachment_value))

        fields.append(("total", amount_value))

        for field_name, value in fields:
            tab_state = multiple_tab_is_active()
            if not tab_state or not tab_state.get("ok"):
                return {
                    "ok": False,
                    "strategy": "physical-row-step",
                    "message": f"Multiple grid is not usable before {field_name} on row {i+1}",
                    "row": row_result,
                    "tab_state": tab_state,
                }

            fresh = refresh_headers()

            if not fresh or not fresh.get("ok"):
                return {
                    "ok": False,
                    "strategy": "physical-row-step",
                    "message": f"Could not refresh grid coordinates before {field_name} on row {i+1}",
                    "row": row_result,
                    "headers": headers,
                    "fresh": fresh,
                }

            fresh_headers = fresh["headers"]

            if field_name == "attachment":
                row_result["debug"].setdefault("attachment", {})
                attachment_result = attach_file_to_row(row_y, value)
                row_result["debug"]["attachment"]["result"] = attachment_result

                if not attachment_result or not attachment_result.get("ok"):
                    return {
                        "ok": False,
                        "strategy": "physical-row-step",
                        "message": f"Could not attach file on row {i+1}",
                        "row": row_result,
                        "headers": fresh_headers,
                    }

                grid_state = wait_for_multiple_grid(timeout_seconds=8.0)
                row_result["debug"]["attachment"]["post_upload_grid"] = grid_state

                if not grid_state or not grid_state.get("ok"):
                    return {
                        "ok": False,
                        "strategy": "physical-row-step",
                        "message": f"Multiple grid did not recover after attachment on row {i+1}",
                        "row": row_result,
                        "headers": fresh_headers,
                    }

                time.sleep(0.20)
                continue

            if field_name == "total":
                # Hit the amount section of TOTAL, not the adjacent GBP selector.
                x = fresh_headers["total"]["left"] + 20
            else:
                x = fresh_headers[field_name]["cx"]

            row_result["debug"].setdefault(field_name, {})
            row_result["debug"][field_name]["geometry"] = {
                "x": x,
                "header": fresh_headers.get(field_name),
                "dialog": fresh.get("dialog"),
            }

            click_result = click_cell(row_y, x, field_name)
            row_result["debug"].setdefault(field_name, {})
            row_result["debug"][field_name]["click"] = click_result

            if not click_result or not click_result.get("ok"):
                return {
                    "ok": False,
                    "strategy": "physical-row-step",
                    "message": f"Could not click {field_name} on row {i+1}",
                    "row": row_result,
                    "headers": headers,
                }

            time.sleep(0.18)

            if field_name == "date":
                date_result = select_date_from_calendar(d)
                row_result["debug"][field_name]["calendar"] = date_result

                if not date_result or not date_result.get("ok"):
                    return {
                        "ok": False,
                        "strategy": "physical-row-step",
                        "message": f"Could not select date on row {i+1}",
                        "row": row_result,
                        "headers": headers,
                    }

                time.sleep(0.16)
                continue

            set_result = set_nearest_editor(row_y, x, field_name, value)
            row_result["debug"][field_name]["set"] = set_result

            if not set_result or not set_result.get("ok"):
                return {
                    "ok": False,
                    "strategy": "physical-row-step",
                    "message": f"Could not edit {field_name} on row {i+1}",
                    "row": row_result,
                    "headers": headers,
                }

            if field_name == "category":
                # Give Expensify's autocomplete a moment to populate.
                time.sleep(0.25)
                option_result = choose_dropdown_option(category_value, row_y, x)
                row_result["debug"][field_name]["option"] = option_result

                if not option_result or not option_result.get("ok"):
                    return {
                        "ok": False,
                        "strategy": "physical-row-step",
                        "message": f"Could not select Category {category_value!r} on row {i+1}",
                        "row": row_result,
                        "headers": headers,
                    }

            time.sleep(0.16)

        results.append(row_result)

    # Return rows in natural top-to-bottom order.
    results.sort(key=lambda r: r["row"])

    final_geometry = refresh_headers()

    return {
        "ok": True,
        "strategy": "physical-row-step",
        "headers": (
            final_geometry.get("headers")
            if final_geometry and final_geometry.get("ok")
            else headers
        ),
        "rows": results,
    }

def save_current_batch():
    """
    Save the current individual expense.

    Receipt uploads can temporarily remove/re-render the Save button. Wait for
    an exact visible Save control to appear before clicking it, then verify the
    expense form closes before continuing to the next CSV row.
    """
    print("  Waiting for SAVE button to become available...")

    find_and_click_js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const norm=s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();

  const candidates=[...document.querySelectorAll(
    'button, input[type="button"], input[type="submit"], a, div, span, [role="button"]'
  )]
    .filter(visible)
    .map(el => {
      const text=norm(
        el.tagName==='INPUT'
          ? (el.value || '')
          : (el.innerText || el.textContent || '')
      );

      const r=el.getBoundingClientRect();
      const cls=String(el.className || '');

      return {
        el,
        text,
        tag:el.tagName,
        cls,
        left:r.left,
        top:r.top,
        width:r.width,
        height:r.height,
        area:r.width*r.height,
        green:/success|green|primary/.test(cls.toLowerCase()) ? 0 : 1,
        buttonish:
          el.tagName==='BUTTON' ||
          el.tagName==='INPUT' ||
          el.tagName==='A' ||
          norm(el.getAttribute('role'))==='button'
            ? 0 : 1
      };
    })
    .filter(x => x.text==='save')
    .sort((a,b) =>
      a.buttonish-b.buttonish ||
      a.green-b.green ||
      a.area-b.area
    );

  if (!candidates.length) {
    return JSON.stringify({
      ok:false,
      message:'No exact visible Save control found'
    });
  }

  const hit=candidates[0];
  const r=hit.el.getBoundingClientRect();
  const x=r.left+r.width/2;
  const y=r.top+r.height/2;

  try {
    hit.el.scrollIntoView({block:'center',inline:'nearest'});
  } catch (_) {}

  const opts={
    bubbles:true,
    cancelable:true,
    clientX:x,
    clientY:y,
    view:window
  };

  try {
    hit.el.dispatchEvent(new MouseEvent('mousedown',opts));
    hit.el.dispatchEvent(new MouseEvent('mouseup',opts));
    hit.el.dispatchEvent(new MouseEvent('click',opts));
  } catch (_) {}

  try { hit.el.click(); } catch (_) {}

  return JSON.stringify({
    ok:true,
    clicked:{
      tag:hit.tag,
      cls:hit.cls,
      text:hit.text,
      left:Math.round(r.left),
      top:Math.round(r.top),
      width:Math.round(r.width),
      height:Math.round(r.height)
    }
  });
})()
"""

    # Receipt processing can briefly remove the Save button.
    # Poll rather than failing immediately.
    deadline = time.time() + 20.0
    click_result = None
    attempts = 0

    while time.time() < deadline:
        attempts += 1
        click_result = _individual_js_json(find_and_click_js)

        if click_result and click_result.get("ok"):
            break

        time.sleep(0.4)

    if not click_result or not click_result.get("ok"):
        # Before declaring failure, determine whether the form may already
        # have closed automatically.
        state_js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const norm=s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();

  const forms=[...document.querySelectorAll('.dialog, .expenseDialog, [role="dialog"]')]
    .filter(visible)
    .filter(el => {
      const txt=norm(el.innerText || el.textContent);
      return (
        txt.includes('merchant') &&
        txt.includes('date') &&
        txt.includes('total') &&
        txt.includes('category') &&
        txt.includes('description')
      );
    });

  return JSON.stringify({
    ok:true,
    expenseForms:forms.length,
    bodyExcerpt:(document.body.innerText || '').replace(/\s+/g,' ').slice(-1200)
  });
})()
"""
        state = _individual_js_json(state_js)

        print("  ✗ SAVE button did not appear within 20 seconds.")
        print(json.dumps({
            "save_search_attempts": attempts,
            "last_save_search": click_result,
            "page_state": state,
        }, indent=2))

        return {
            "ok": False,
            "stage": "wait-for-save",
            "attempts": attempts,
            "details": click_result,
            "page_state": state,
        }

    print(
        "  ✓ SAVE button appeared and was clicked "
        f"({click_result.get('clicked', {}).get('tag', '?')}) "
        f"after {attempts} check(s)."
    )

    # Verify the expense form closes after Save.
    deadline = time.time() + 20.0
    last_state = None

    while time.time() < deadline:
        check_js = r"""
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const norm=s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();

  const visibleExpenseForms=[...document.querySelectorAll(
    '.dialog, .expenseDialog, [role="dialog"]'
  )]
    .filter(visible)
    .filter(el => {
      const txt=norm(el.innerText || el.textContent);
      return (
        txt.includes('merchant') &&
        txt.includes('date') &&
        txt.includes('total') &&
        txt.includes('category') &&
        txt.includes('description')
      );
    });

  const visibleSave=[...document.querySelectorAll(
    'button, input[type="button"], input[type="submit"], a, div, span, [role="button"]'
  )]
    .filter(visible)
    .filter(el => {
      const txt=norm(
        el.tagName==='INPUT'
          ? (el.value || '')
          : (el.innerText || el.textContent || '')
      );
      return txt==='save';
    });

  return JSON.stringify({
    ok:visibleExpenseForms.length===0 && visibleSave.length===0,
    visibleExpenseForms:visibleExpenseForms.length,
    visibleSaveControls:visibleSave.length
  });
})()
"""
        last_state = _individual_js_json(check_js)

        if last_state and last_state.get("ok"):
            print("  ✓ SAVE confirmed: expense form closed.")
            time.sleep(1.0)
            return {
                "ok": True,
                "click": click_result,
                "confirmation": last_state,
                "save_wait_attempts": attempts,
            }

        time.sleep(0.35)

    print("  ✗ SAVE was clicked, but the expense form still appears open.")
    print("    Stopping so the next CSV row is not started.")

    return {
        "ok": False,
        "stage": "confirm-save",
        "click": click_result,
        "confirmation": last_state,
    }




def cleanup_uncategorized_to_subsistence(max_items=50):
    print("\nFINAL CATEGORY CHECK")
    print("  Looking for Uncategorized expenses...")

    safari_navigate(REPORT_URL)
    wait_ready()
    time.sleep(1.0)

    changed = 0

    for attempt in range(max_items):
        find_js = r'''
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const norm=s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();

  const exact=[...document.querySelectorAll('body *')]
    .filter(visible)
    .filter(el => norm(el.innerText || el.textContent)==='uncategorized')
    .map(el => {
      let container=el;

      for (let i=0; i<6 && container.parentElement; i++) {
        const p=container.parentElement;
        const pr=p.getBoundingClientRect();
        const txt=norm(p.innerText || p.textContent);

        if (
          pr.width >= 250 &&
          pr.height >= 25 &&
          pr.height <= 220 &&
          txt.includes('uncategorized')
        ) {
          container=p;
        } else if (pr.height > 220) {
          break;
        } else {
          container=p;
        }
      }

      const cr=container.getBoundingClientRect();

      return {
        left:cr.left,
        top:cr.top,
        width:cr.width,
        height:cr.height,
        area:cr.width*cr.height,
        text:(container.innerText || container.textContent || '')
          .replace(/\s+/g,' ')
          .trim()
          .slice(0,500)
      };
    })
    .sort((a,b) => a.top-b.top || a.area-b.area);

  if (!exact.length) {
    return JSON.stringify({
      ok:true,
      found:false,
      count:0
    });
  }

  const hit=exact[0];
  const x=hit.left + hit.width*0.45;
  const y=hit.top + hit.height/2;

  return JSON.stringify({
    ok:true,
    found:true,
    count:exact.length,
    target:{
      text:hit.text,
      left:Math.round(hit.left),
      top:Math.round(hit.top),
      width:Math.round(hit.width),
      height:Math.round(hit.height),
      clickX:Math.round(x),
      clickY:Math.round(y)
    }
  });
})()
'''
        found = _individual_js_json(find_js)

        if not found or not found.get("ok"):
            return {
                "ok": False,
                "stage": "scan",
                "changed": changed,
                "details": found,
            }

        if not found.get("found"):
            if changed:
                print(
                    f"  ✓ Category cleanup complete: changed {changed} "
                    "expense(s) from Uncategorized to Subsistence."
                )
            else:
                print("  ✓ No Uncategorized expenses found.")
            return {"ok": True, "changed": changed}

        target = found.get("target", {})
        print(
            f"  Found Uncategorized expense "
            f"({found.get('count', 1)} currently visible)."
        )

        click_script = f'''
tell application "Safari"
    activate
end tell
tell application "System Events"
    tell process "Safari"
        click at {{{int(target.get("clickX", 0))}, {int(target.get("clickY", 0))}}}
    end tell
end tell
'''

        try:
            run_osascript(click_script)
        except Exception as e:
            return {
                "ok": False,
                "stage": "open-expense",
                "changed": changed,
                "message": str(e),
                "target": target,
            }

        time.sleep(0.8)

        form_js = r'''
(() => {
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el), r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const norm=s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();

  const forms=[...document.querySelectorAll(
    '.dialog, .expenseDialog, [role="dialog"]'
  )]
    .filter(visible)
    .filter(el => {
      const txt=norm(el.innerText || el.textContent);
      return (
        txt.includes('merchant') &&
        txt.includes('date') &&
        txt.includes('total') &&
        txt.includes('category')
      );
    });

  return JSON.stringify({
    ok:forms.length>0,
    forms:forms.length
  });
})()
'''
        form_state = None
        for _ in range(15):
            form_state = _individual_js_json(form_js)
            if form_state and form_state.get("ok"):
                break
            time.sleep(0.2)

        if not form_state or not form_state.get("ok"):
            return {
                "ok": False,
                "stage": "open-expense",
                "changed": changed,
                "message": "Clicked Uncategorized expense but edit form did not open",
                "target": target,
                "form_state": form_state,
            }

        category_result = select_individual_category("Subsistence")
        if not category_result or not category_result.get("ok"):
            return {
                "ok": False,
                "stage": "set-category",
                "changed": changed,
                "target": target,
                "details": category_result,
            }

        print("  ✓ Category changed to Subsistence; saving...")

        save_result = save_current_batch()
        if not save_result or not save_result.get("ok"):
            return {
                "ok": False,
                "stage": "save-cleanup",
                "changed": changed,
                "target": target,
                "details": save_result,
            }

        changed += 1
        print(f"  ✓ Cleanup expense {changed} saved.")

        safari_navigate(REPORT_URL)
        wait_ready()
        time.sleep(0.8)

    return {
        "ok": False,
        "stage": "limit",
        "changed": changed,
        "message": (
            f"Stopped after {max_items} cleanup attempts to avoid an "
            "unexpected infinite loop."
        ),
    }





def parse_report_row_date(date_text, csv_expenses):
    """Resolve a report-row date such as 'Jun 1' against the CSV."""
    raw = (date_text or "").strip()

    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%b %d, %Y",
        "%b %d %Y",
    ):
        try:
            return datetime.strptime(raw, fmt).date()
        except Exception:
            pass

    matches = []
    raw_lower = raw.lower()

    for row in csv_expenses:
        variants = {
            row["date"].strftime("%b %-d").lower(),
            row["date"].strftime("%b %d").lower(),
            row["date"].strftime("%d %b").lower(),
            row["date"].strftime("%d %b %Y").lower(),
            row["date"].strftime("%b %-d %Y").lower(),
        }
        if raw_lower in variants:
            matches.append(row["date"])

    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None



def clean_report_merchant(value):
    """
    Remove Expensify UI helper text from merchant strings.

    Example:
      Edit This ExpensePret a MangerEdit Expense
      -> Pret a Manger
    """
    s = (value or "").strip()

    for junk in (
        "Edit This Expense",
        "Edit Expense",
        "View Expense",
        "Open Expense",
    ):
        s = s.replace(junk, "")

    return " ".join(s.split()).strip()


def normalize_match_text(value):
    """
    Normalize text for matching while ignoring punctuation/case differences.

    Example:
      Sainsbury's -> sainsburys
      Sainsburys  -> sainsburys
    """
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())



def match_report_row_to_csv(row_info, csv_expenses):
    """
    Match a report row to exactly one CSV row using:
      date + cleaned merchant + amount

    Merchant matching ignores Expensify edit-helper text, punctuation and case.
    Per Diem is always excluded.
    """
    merchant_raw = (row_info.get("merchant") or "").strip()
    merchant = clean_report_merchant(merchant_raw)
    merchant_norm = normalize_match_text(merchant)

    if merchant_norm == "perdiem":
        return None

    total_raw = (row_info.get("total") or "").replace(",", "")
    m = re.search(r"(-?\d+(?:\.\d{1,2})?)", total_raw)
    amount = f"{float(m.group(1)):.2f}" if m else None

    parsed_date = parse_report_row_date(
        row_info.get("date") or "",
        csv_expenses,
    )

    candidates = []

    for row in csv_expenses:
        row_merchant = row["merchant"].strip()
        row_category = row["category"].strip().lower()

        if normalize_match_text(row_merchant) == "perdiem":
            continue
        if normalize_match_text(row_category) == "perdiem":
            continue
        if not row.get("receipt_path"):
            continue

        if parsed_date and row["date"] != parsed_date:
            continue
        if amount and row["amount"] != amount:
            continue

        if merchant_norm:
            if normalize_match_text(row_merchant) != merchant_norm:
                continue

        candidates.append(row)

    return candidates[0] if len(candidates) == 1 else None



def find_next_report_row_with_green_plus():
    """
    Robust row-first green-plus detection.

    Scans all report-like expense rows, then inspects the middle TAG area for
    green styling on normal elements AND ::before/::after pseudo-elements.
    This avoids relying on literal '+' text or Expensify-specific class names.
    """
    js = r"""
(() => {
  const cssVisible = el => {
    if (!el || el.nodeType !== 1) return false;
    const s=getComputedStyle(el);
    const r=el.getBoundingClientRect();
    return s.display!=='none' &&
           s.visibility!=='hidden' &&
           s.opacity!=='0' &&
           r.width>0 &&
           r.height>0;
  };

  const clean=s => (s||'').replace(/\s+/g,' ').trim();
  const norm=s => clean(s).toLowerCase();

  function rgbTuple(v) {
    const m=String(v||'').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/i);
    return m ? [Number(m[1]),Number(m[2]),Number(m[3])] : null;
  }

  function greenValue(v) {
    const rgb=rgbTuple(v);
    if (!rgb) return false;
    const [r,g,b]=rgb;
    return (
      g >= 110 &&
      g > r * 1.20 &&
      g > b * 1.08
    );
  }

  function styleLooksGreen(style) {
    if (!style) return false;
    const props=[
      style.color,
      style.backgroundColor,
      style.borderTopColor,
      style.borderRightColor,
      style.borderBottomColor,
      style.borderLeftColor,
      style.fill,
      style.stroke,
      style.outlineColor
    ];
    return props.some(greenValue);
  }

  function elementOrPseudoLooksGreen(el) {
    try {
      if (styleLooksGreen(getComputedStyle(el))) return true;
    } catch (_) {}

    for (const pseudo of ['::before','::after']) {
      try {
        const ps=getComputedStyle(el,pseudo);
        if (styleLooksGreen(ps)) return true;

        const content=(ps.content || '').replace(/^["']|["']$/g,'');
        if (content==='+' || content==='＋') return true;
      } catch (_) {}
    }

    const sem=norm(
      String(el.className || '') + ' ' +
      (el.getAttribute?.('style') || '') + ' ' +
      (el.getAttribute?.('fill') || '') + ' ' +
      (el.getAttribute?.('stroke') || '') + ' ' +
      (el.getAttribute?.('title') || '') + ' ' +
      (el.getAttribute?.('aria-label') || '')
    );

    return /green|success|emerald|lime|receipt.*missing|missing.*receipt/.test(sem);
  }

  function descendantsIncludingSelf(el) {
    return [el, ...el.querySelectorAll('*')];
  }

  function rowLooksLikeExpense(el) {
    if (!cssVisible(el)) return false;

    const r=el.getBoundingClientRect();
    const txt=clean(el.innerText || el.textContent);

    return (
      r.width >= 650 &&
      r.height >= 34 &&
      r.height <= 150 &&
      /£\s*\d+(?:\.\d{2})?/.test(txt) &&
      /\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b/i.test(txt)
    );
  }

  // Gather table rows first.
  let candidates=[...document.querySelectorAll('tr,[role="row"]')]
    .filter(rowLooksLikeExpense);

  // Also gather DIV-based rows.
  const divRows=[...document.querySelectorAll('div')]
    .filter(rowLooksLikeExpense)
    .filter(el => {
      const er=el.getBoundingClientRect();

      // Keep minimal row-like containers, not giant wrappers containing
      // multiple complete expense rows.
      return ![...el.children].some(ch => {
        if (!rowLooksLikeExpense(ch)) return false;
        const cr=ch.getBoundingClientRect();
        return cr.width >= er.width*0.75;
      });
    });

  candidates.push(...divRows);

  // De-duplicate near-identical row rectangles.
  const rows=[];
  for (const row of candidates) {
    const r=row.getBoundingClientRect();
    const duplicate=rows.some(x => {
      const xr=x.getBoundingClientRect();
      return (
        Math.abs(xr.top-r.top)<3 &&
        Math.abs(xr.height-r.height)<3 &&
        Math.abs(xr.left-r.left)<5
      );
    });
    if (!duplicate) rows.push(row);
  }

  const analyzed=[];

  for (const row of rows) {
    const rr=row.getBoundingClientRect();
    const rowText=clean(row.innerText || row.textContent);
    const rowNorm=norm(rowText);

    // Skip Per Diem lines entirely.
    if (rowNorm.includes('per diem')) {
      analyzed.push({
        rowText,
        skipped:'per-diem',
        top:rr.top
      });
      continue;
    }

    // Date
    let dateText='';
    const dateMatches=rowText.match(
      /\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b/i
    );
    if (dateMatches) dateText=dateMatches[0];

    // Total: last £ amount in row.
    const totals=[...rowText.matchAll(/£\s*\d+(?:\.\d{2})?/g)]
      .map(m=>m[0]);
    const totalText=totals.length ? totals[totals.length-1] : '';

    // Find small text fragments positioned immediately to the right of date
    // to infer Merchant.
    let merchantText='';

    const allEls=descendantsIncludingSelf(row)
      .filter(cssVisible)
      .map(el => {
        const r=el.getBoundingClientRect();
        return {
          el,
          txt:clean(el.innerText || el.textContent),
          left:r.left,
          right:r.right,
          cy:r.top+r.height/2,
          width:r.width,
          height:r.height,
          area:r.width*r.height
        };
      });

    const dateEls=allEls
      .filter(x => x.txt===dateText)
      .sort((a,b)=>a.area-b.area);

    if (dateEls.length) {
      const d=dateEls[0];
      const possible=allEls
        .filter(x =>
          x.txt &&
          x.left > d.right + 10 &&
          x.left < rr.left + rr.width*0.55 &&
          Math.abs(x.cy-d.cy)<25 &&
          x.width < rr.width*0.35
        )
        .sort((a,b)=>a.left-b.left || a.area-b.area);

      merchantText=possible[0]?.txt || '';
    }

    merchantText=merchantText
      .replace(/Edit This Expense/gi,'')
      .replace(/Edit Expense/gi,'')
      .replace(/View Expense/gi,'')
      .replace(/Open Expense/gi,'')
      .replace(/\s+/g,' ')
      .trim();

    // TAG area: screenshot/layout places it roughly in the middle of row,
    // around 43%-66% of width. Inspect everything whose center falls there.
    const tagLeft=rr.left + rr.width*0.40;
    const tagRight=rr.left + rr.width*0.68;

    const tagEls=allEls
      .filter(x => {
        const cx=x.left+x.width/2;
        return (
          cx >= tagLeft &&
          cx <= tagRight &&
          Math.abs(x.cy-(rr.top+rr.height/2)) <= rr.height*0.55
        );
      });

    const greenHits=[];

    for (const x of tagEls) {
      if (!elementOrPseudoLooksGreen(x.el)) continue;

      // Find the smallest clickable-ish ancestor near this green decoration.
      let target=x.el;
      let cur=x.el;

      for (let depth=0; depth<5 && cur; depth++,cur=cur.parentElement) {
        if (!cssVisible(cur)) continue;
        const r=cur.getBoundingClientRect();
        const clickable =
          cur.tagName==='BUTTON' ||
          cur.tagName==='A' ||
          norm(cur.getAttribute?.('role'))==='button' ||
          getComputedStyle(cur).cursor==='pointer' ||
          typeof cur.onclick==='function';

        if (
          clickable &&
          r.width<=100 &&
          r.height<=100
        ) {
          target=cur;
          break;
        }
      }

      const tr=target.getBoundingClientRect();
      const cx=tr.left+tr.width/2;
      const cy=tr.top+tr.height/2;

      // Keep controls geographically in the TAG band.
      if (cx < tagLeft || cx > tagRight) continue;

      greenHits.push({
        source:x.el,
        target,
        left:tr.left,
        top:tr.top,
        width:tr.width,
        height:tr.height,
        cx,
        cy,
        area:tr.width*tr.height,
        cls:String(target.className || ''),
        txt:clean(target.innerText || target.textContent)
      });
    }

    // De-duplicate nested green hits.
    greenHits.sort((a,b)=>a.area-b.area);
    const uniqueGreen=[];
    for (const g of greenHits) {
      if (
        !uniqueGreen.some(u =>
          Math.abs(u.cx-g.cx)<5 &&
          Math.abs(u.cy-g.cy)<5
        )
      ) {
        uniqueGreen.push(g);
      }
    }

    analyzed.push({
      row,
      rr,
      rowText,
      date:dateText,
      merchant:merchantText,
      total:totalText,
      green:uniqueGreen,
      top:rr.top
    });
  }

  const missing=analyzed
    .filter(x => x.row && x.green && x.green.length)
    .sort((a,b)=>a.top-b.top);

  const debug=analyzed.map(x => ({
    text:(x.rowText || '').slice(0,260),
    date:x.date || '',
    merchant:x.merchant || '',
    total:x.total || '',
    greenCount:x.green ? x.green.length : 0,
    skipped:x.skipped || ''
  }));

  if (!missing.length) {
    return JSON.stringify({
      ok:true,
      found:false,
      rowsScanned:analyzed.length,
      missingDetected:0,
      debugRows:debug.slice(0,120)
    });
  }

  const hit=missing[0];
  const g=hit.green[0];

  return JSON.stringify({
    ok:true,
    found:true,
    rowsScanned:analyzed.length,
    missingDetected:missing.length,
    row:{
      date:hit.date,
      merchant:hit.merchant,
      total:hit.total,
      text:hit.rowText.slice(0,700)
    },
    plus:{
      tag:g.target.tagName,
      cls:g.cls.slice(0,180),
      text:g.txt.slice(0,100),
      left:Math.round(g.left),
      top:Math.round(g.top),
      width:Math.round(g.width),
      height:Math.round(g.height),
      clickX:Math.round(g.cx),
      clickY:Math.round(g.cy)
    },
    debugRows:debug.slice(0,120)
  });
})()
"""
    return _individual_js_json(js)



def click_report_green_plus(plus_info):
    """Click the report row's green plus via macOS System Events."""
    x = int(plus_info["clickX"])
    y = int(plus_info["clickY"])

    script = f"""
tell application "Safari"
    activate
end tell
tell application "System Events"
    tell process "Safari"
        click at {{{x}, {y}}}
    end tell
end tell
"""

    try:
        run_osascript(script)
    except Exception as exc:
        return {
            "ok": False,
            "message": str(exc),
            "plus": plus_info,
        }

    return {"ok": True, "plus": plus_info}


def repair_missing_report_receipts(csv_expenses, max_items=200):
    """
    Scan report rows directly for green missing-receipt plus controls.

    For each non-Per-Diem row:
      - read date / merchant / total from the report row
      - uniquely match it to the CSV
      - click that exact green plus
      - click Import From Computer
      - upload receipt_path
      - reload the report and continue
    """
    print("\nFINAL RECEIPT CHECK")
    print("  Scanning every report line for a small green missing-receipt control...")
    print("  Per Diem lines are skipped.")

    safari_navigate(REPORT_URL)
    wait_ready()
    time.sleep(1.0)

    repaired = 0
    unmatched = set()

    for _ in range(max_items):
        found = find_next_report_row_with_green_plus()

        if not found or not found.get("ok"):
            return {
                "ok": False,
                "stage": "scan-report",
                "repaired": repaired,
                "details": found,
            }

        if not found.get("found"):
            rows_scanned = found.get("rowsScanned", 0)
            missing_detected = found.get("missingDetected", 0)

            print(
                f"  Scan result: {rows_scanned} expense row(s) inspected; "
                f"{missing_detected} green missing-receipt control(s) detected."
            )
            print(
                f"  ✓ Receipt repair complete: uploaded {repaired} "
                "missing receipt(s)."
            )

            # Include row diagnostics in the returned result so a false zero
            # can be diagnosed without changing the script again.
            return {
                "ok": True,
                "repaired": repaired,
                "rows_scanned": rows_scanned,
                "missing_detected": missing_detected,
                "debug_rows": found.get("debugRows", []),
            }

        row = found["row"]
        plus = found["plus"]

        cleaned_merchant = clean_report_merchant(
            row.get("merchant") or ""
        )
        row["merchant"] = cleaned_merchant

        signature = (
            (row.get("date") or "").strip().lower(),
            normalize_match_text(cleaned_merchant),
            (row.get("total") or "").strip().lower(),
        )

        print(
            f"  Detector: scanned {found.get('rowsScanned', '?')} row(s), "
            f"found {found.get('missingDetected', '?')} missing receipt row(s)."
        )
        print(
            "  Green + row: "
            f"{row.get('date')} | "
            f"{cleaned_merchant} | "
            f"{row.get('total')}"
        )

        if normalize_match_text(cleaned_merchant) == "perdiem":
            print("  Skipping Per Diem line.")
            _individual_js_json(r"""
(() => {
  window.scrollBy(0,110);
  return JSON.stringify({ok:true});
})()
""")
            time.sleep(0.25)
            continue

        matched = match_report_row_to_csv(row, csv_expenses)

        if not matched:
            if signature in unmatched:
                return {
                    "ok": False,
                    "stage": "match-csv",
                    "repaired": repaired,
                    "message": (
                        "The same unmatched report line was found again. "
                        "Stopping to avoid looping."
                    ),
                    "row": row,
                }

            unmatched.add(signature)
            print(
                "  Skipping: no unique CSV match for "
                "date + merchant + amount."
            )
            _individual_js_json(r"""
(() => {
  window.scrollBy(0,110);
  return JSON.stringify({ok:true});
})()
""")
            time.sleep(0.25)
            continue

        receipt_path = matched["receipt_path"]
        rp = Path(receipt_path).expanduser()

        if not rp.is_file():
            return {
                "ok": False,
                "stage": "receipt-file-missing",
                "repaired": repaired,
                "row": row,
                "message": f"Receipt file does not exist: {rp}",
            }

        print(
            "  ✓ CSV match: "
            f"{matched['date'].isoformat()} | "
            f"{matched['merchant']} | "
            f"£{matched['amount']}"
        )
        print(f"  ✓ Uploading: {receipt_path}")

        plus_result = click_report_green_plus(plus)
        if not plus_result or not plus_result.get("ok"):
            return {
                "ok": False,
                "stage": "click-green-plus",
                "repaired": repaired,
                "row": row,
                "details": plus_result,
            }

        time.sleep(0.4)

        import_result = click_individual_import_from_computer()
        if not import_result or not import_result.get("ok"):
            return {
                "ok": False,
                "stage": "import-from-computer",
                "repaired": repaired,
                "row": row,
                "details": import_result,
            }

        time.sleep(0.4)

        file_result = choose_individual_file(receipt_path)
        if not file_result or not file_result.get("ok"):
            return {
                "ok": False,
                "stage": "file-picker",
                "repaired": repaired,
                "row": row,
                "details": file_result,
            }

        # The report-page receipt upload is saved directly by Expensify.
        time.sleep(1.5)
        repaired += 1
        print(f"  ✓ Receipt {repaired} uploaded.")

        safari_navigate(REPORT_URL)
        wait_ready()
        time.sleep(0.8)

    return {
        "ok": False,
        "stage": "limit",
        "repaired": repaired,
        "message": (
            f"Stopped after {max_items} repair attempts to avoid a loop."
        ),
    }



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto-save", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--test-first-row",
        action="store_true",
        help="Only fill the first row of the first batch, then stop before Save"
    )
    ap.add_argument(
        "--url",
        default=DEFAULT_REPORT_URL,
        help="Expensify report URL to use instead of the default report"
    )
    ap.add_argument(
        "--start",
        default=DEFAULT_START_DATE.isoformat(),
        help="First expense date in YYYY-MM-DD format"
    )
    ap.add_argument(
        "--end",
        default=DEFAULT_END_DATE.isoformat(),
        help="Last expense date in YYYY-MM-DD format"
    )
    ap.add_argument(
        "--attachment",
        help="Optional local file to attach to every expense row"
    )
    ap.add_argument(
        "--csv",
        help=(
            "CSV containing report_url,date,merchant,amount,currency,"
            "category,description,receipt_path"
        )
    )
    ap.add_argument(
        "--resume-csv",
        action="store_true",
        help=(
            "Deprecated compatibility flag; CSV mode now checks saved-state "
            "by default."
        )
    )
    ap.add_argument(
        "--skip-category-cleanup",
        action="store_true",
        help=(
            "Skip the final report check that changes Uncategorized expenses "
            "to Subsistence."
        )
    )
    ap.add_argument(
        "--skip-receipt-repair",
        action="store_true",
        help=(
            "Skip the final pass that re-uploads missing receipts for "
            "Subsistence expenses by matching them to the CSV. Per Diem is always ignored."
        )
    )
    ap.add_argument(
        "--diagnose",
        action="store_true",
        help="Only test Add Expenses > New Expense > Multiple, then stop"
    )
    ap.add_argument(
        "--diagnose-native",
        action="store_true",
        help="Test native Add Expenses > New Expense > Multiple navigation"
    )
    ap.add_argument(
        "--diagnose-focus",
        action="store_true",
        help="Test DOM focus + native Enter navigation only"
    )
    ap.add_argument(
        "--diagnose-logging",
        action="store_true",
        help="Run navigation with verbose DOM logging, enter no expense data"
    )
    args = ap.parse_args()

    global REPORT_URL, START_DATE, END_DATE, ATTACHMENT_FILE
    REPORT_URL = args.url.strip()

    if not REPORT_URL.startswith("https://www.expensify.com/report"):
        raise SystemExit(
            "ERROR: --url must be an Expensify report URL beginning with "
            "https://www.expensify.com/report"
        )

    try:
        START_DATE = date.fromisoformat(args.start)
        END_DATE = date.fromisoformat(args.end)
    except ValueError:
        raise SystemExit(
            "ERROR: --start and --end must use YYYY-MM-DD format, "
            "for example --start 2026-05-01 --end 2026-05-31"
        )

    if END_DATE < START_DATE:
        raise SystemExit("ERROR: --end cannot be earlier than --start")

    if args.attachment:
        attachment_path = Path(args.attachment).expanduser().resolve()
        if not attachment_path.is_file():
            raise SystemExit(
                f"ERROR: attachment file does not exist: {attachment_path}"
            )
        ATTACHMENT_FILE = str(attachment_path)
    else:
        ATTACHMENT_FILE = None

    csv_expenses = None
    if args.csv:
        csv_expenses = load_expenses_csv(args.csv)
        REPORT_URL = csv_expenses[0]["report_url"]
        START_DATE = min(r["date"] for r in csv_expenses)
        END_DATE = max(r["date"] for r in csv_expenses)

    print("\nEXPENSIFY - EXISTING SAFARI SESSION")
    print("=" * 42)
    if csv_expenses:
        expense_count = len(csv_expenses)
        total_value = sum(float(r["amount"]) for r in csv_expenses)
    else:
        expense_count = (END_DATE - START_DATE).days + 1
        total_value = expense_count * float(AMOUNT)

    print(f"Report URL:  {REPORT_URL}")
    print(
        f"Dates:       {START_DATE.strftime('%d/%m/%Y')} -> "
        f"{END_DATE.strftime('%d/%m/%Y')}"
    )

    if csv_expenses:
        print(f"Input CSV:   {Path(args.csv).expanduser().resolve()}")
        print(f"Expenses:    {expense_count}")
        print(f"Total:       £{total_value:.2f}")
        print("Details:     read per row from CSV")
        print("Receipts:    UPLOADED FIRST + REQUIRED before Save for non-Per-Diem expenses")
        if args.test_first_row:
            print("CSV mode:    TEST - first individual expense only")
        elif args.resume_csv:
            print("CSV mode:    INDIVIDUAL EXPENSES + STATE CHECK")
        else:
            print("CSV mode:    INDIVIDUAL EXPENSES + STATE CHECK + SAVE EACH ONE")
    else:
        print(f"Merchant:    {MERCHANT}")
        print(f"Amount:      £{AMOUNT}")
        print(f"Description: {DESCRIPTION}")
        print(f"Attachment:  {ATTACHMENT_FILE or 'None'}")
        print(f"Expenses:    {expense_count}")
        print(f"Total:       £{total_value:.2f}")
    print("Submit:      NEVER")
    print("=" * 42)

    if not safari_exists():
        print("\nSafari is not running.")
        return 2

    try:
        print(f"\nCurrent Safari tab: {safari_title()}")
        current = safari_url()
        print(f"Current URL: {current}")
    except Exception as exc:
        print(f"Could not access Safari: {exc}")
        return 2

    if current != REPORT_URL:
        print("\nOpening target Expensify report in the current Safari tab...")
        safari_navigate(REPORT_URL)
        wait_ready()
        time.sleep(2)

    try:
        host = safari_js("document.location.hostname")
    except Exception as exc:
        print("\nSafari refused JavaScript from Apple Events.")
        print(
            "Enable Safari > Settings > Developer > "
            "Allow JavaScript from Apple Events"
        )
        print(f"Details: {exc}")
        return 3

    if "expensify" not in host.lower():
        print(f"Unexpected hostname: {host}")
        return 3

    print("\nSafari control is working.")

    input(
        "\nMake sure the report is visible in Safari. "
        "Press Enter to continue..."
    )

    if args.diagnose or args.diagnose_native or args.diagnose_focus or args.diagnose_logging:
        print("\nSINGLE-CLICK EXPENSIFY DIAGNOSTIC")
        print("Testing Add Expenses -> New Expense -> Multiple")
        try:
            open_create_multiple()
        except Exception as exc:
            print("\nDIAGNOSTIC FAILED")
            print(exc)
            return 6

        print("\nDIAGNOSTIC PASSED")
        print("The Multiple expense screen should now be open.")
        print("No expense data was entered.")
        return 0

    saved = load_state()

    if csv_expenses:
        all_items = csv_expenses

        # CSV mode now skips expenses already confirmed saved.
        # The state key includes report URL + full expense details, so
        # identical dates on different reports do not collide.
        if args.force:
            pending = list(all_items)
        else:
            pending = [
                r for r in all_items
                if expense_state_key(r) not in saved
            ]
    else:
        all_items = june_dates()
        pending = all_items if args.force else [
            d for d in all_items
            if expense_state_key(d) not in saved
        ]

    if not pending:
        if csv_expenses:
            print(
                "\nAll CSV expenses for this report are already recorded as saved."
            )

            if not args.skip_category_cleanup:
                cleanup_result = cleanup_uncategorized_to_subsistence()
                if not cleanup_result or not cleanup_result.get("ok"):
                    print("\nCATEGORY CLEANUP FAILED")
                    print(json.dumps(cleanup_result, indent=2))
                    return 7

            if not args.skip_receipt_repair:
                receipt_result = repair_missing_report_receipts(csv_expenses)
                if not receipt_result or not receipt_result.get("ok"):
                    print("\nRECEIPT REPAIR FAILED")
                    print(json.dumps(receipt_result, indent=2, default=str))
                    return 8

            print(
                "Use --force only if you deliberately want to create duplicates."
            )
        else:
            print(
                "\nAll requested expenses for this report are already recorded as saved."
            )
        return 0

    if csv_expenses and not args.force:
        skipped_count = len(all_items) - len(pending)
        if skipped_count:
            print(
                f"\nState check: skipping {skipped_count} expense(s) "
                "already confirmed saved."
            )

    if args.test_first_row:
        pending = pending[:1]
        print("\nTEST MODE: only the first expense row will be filled.")

    # CSV mode now creates one normal Expense dialog per CSV row.
    # This avoids all Multiple-grid row-position and receipt-state problems.
    if csv_expenses:
        for i, item in enumerate(pending, 1):
            print(
                f"\nEXPENSE {i}/{len(pending)}: "
                f"{item['date']:%d/%m/%Y}  "
                f"{item['merchant']}  £{item['amount']}  "
                f"{item['category']}"
            )

            safari_navigate(REPORT_URL)
            wait_ready()
            time.sleep(1.0)

            try:
                open_create_individual()
            except Exception as exc:
                print("\nFAILED TO OPEN INDIVIDUAL EXPENSE")
                print(exc)
                return 5

            print("  Filling individual expense...")
            result = fill_individual_expense(item)

            if not result.get("ok"):
                print("\nINDIVIDUAL EXPENSE FILL FAILED")
                print(json.dumps(result, indent=2))
                print(
                    "\nLeave Safari on this screen and send me the output above."
                )
                return 5

            print(
                f"  ✓ Filled {item['date']:%d/%m/%Y} "
                f"{item['merchant']} £{item['amount']}"
            )

            # Safety gate: never Save a non-Per-Diem expense unless its
            # receipt upload was successfully completed.
            item_category = (item.get("category") or "").strip().lower()
            item_merchant = (item.get("merchant") or "").strip().lower()
            item_is_per_diem = (
                item_category == "per diem" or
                item_merchant == "per diem"
            )

            if not item_is_per_diem and not result.get("receipt_validated"):
                print("\nERROR: Receipt validation failed.")
                print(
                    "This is not a Per Diem expense, so it will NOT be saved "
                    "without a confirmed receipt upload."
                )
                print(
                    f"Expense: {item['date']:%d/%m/%Y} | "
                    f"{item['merchant']} | £{item['amount']} | "
                    f"{item['category']}"
                )
                return 9

            if not item_is_per_diem:
                print("  ✓ Receipt validated: Save is now allowed.")
            else:
                print("  ✓ Per Diem: receipt validation not required.")

            if args.test_first_row and not args.auto_save:
                print("\nTEST MODE: expense filled but NOT saved.")
                print("Review it in Safari.")
                return 0

            print("  Saving this individual expense before continuing...")
            save_result = save_current_batch()

            if not save_result or not save_result.get("ok"):
                print("\nERROR: Expense was not confirmed saved.")
                print("Stopping before the next CSV row.")
                return 6

            print(
                f"  ✓ Expense {i}/{len(pending)} SAVED successfully. "
                "Moving to next CSV row."
            )

            saved.add(expense_state_key(item))
            save_state(saved)

            # Let the report settle before opening the next expense.
            time.sleep(0.8)

        if not args.skip_category_cleanup:
            cleanup_result = cleanup_uncategorized_to_subsistence()
            if not cleanup_result or not cleanup_result.get("ok"):
                print("\nCATEGORY CLEANUP FAILED")
                print(json.dumps(cleanup_result, indent=2))
                print(
                    "The CSV expenses were processed, but the final "
                    "Uncategorized cleanup did not complete."
                )
                return 7

        if not args.skip_receipt_repair:
            receipt_result = repair_missing_report_receipts(csv_expenses)
            if not receipt_result or not receipt_result.get("ok"):
                print("\nRECEIPT REPAIR FAILED")
                print(json.dumps(receipt_result, indent=2, default=str))
                print(
                    "The CSV expenses were processed, but the final "
                    "missing-receipt repair did not complete."
                )
                return 8

        print("\nDONE")
        print(f"{len(pending)} individual expense(s) processed.")
        print("Each expense was saved before the next CSV row was started.")
        print(
            "Non-Per-Diem expenses had receipts uploaded first and were only "
            "saved after receipt validation succeeded."
        )
        if not args.skip_category_cleanup:
            print(
                "Final category check completed: Uncategorized expenses "
                "were changed to Subsistence."
            )
        if not args.skip_receipt_repair:
            print(
                "Final receipt check completed: report lines with a green receipt + "
                "were matched to the CSV and repaired. Per Diem was skipped."
            )
        print("The report has NOT been submitted.")
        safari_navigate(REPORT_URL)
        return 0

    batches = list(chunks(pending, BATCH_SIZE))

    for i, batch in enumerate(batches, 1):
        first_date = (
            batch[0]["date"] if isinstance(batch[0], dict) else batch[0]
        )
        last_date = (
            batch[-1]["date"] if isinstance(batch[-1], dict) else batch[-1]
        )

        print(
            f"\nBATCH {i}/{len(batches)}: "
            f"{first_date:%d/%m/%Y} -> {last_date:%d/%m/%Y}"
        )

        safari_navigate(REPORT_URL)
        wait_ready()
        time.sleep(1.5)

        open_create_multiple()

        print("  Filling expense data...")
        result = fill_batch(batch)

        if not result.get("ok"):
            print("\nFIELD MAPPING FAILED")
            print(json.dumps(result, indent=2))
            print(
                "\nLeave Safari on this screen and send me the output above. "
                "I can map the exact Expensify fields from it."
            )
            return 5

        print(
            f"  ✓ Filled {len(batch)} rows "
            f"using {result.get('strategy')}"
        )

        for row in result.get("rows", []):
            print(
                f"    row {row.get('row')}: "
                f"{row.get('date')}  "
                f"{row.get('merchant')}  "
                f"£{row.get('total')}  "
                f"Category={row.get('category', 'Per Diem')}  "
                f"{row.get('description')}"
            )

        if not result.get("rows"):
            for item in batch:
                if isinstance(item, dict):
                    print(
                        f"    {item['date']:%d/%m/%Y}  "
                        f"{item['merchant']}  £{item['amount']}  "
                        f"{item['category']}"
                    )
                else:
                    print(f"    {item:%d/%m/%Y}  Per Diem  £15.00  Per Diem")

        # CSV mode is intended to run unattended through the entire file.
        # For safety, --test-first-row still preserves the manual SAVE prompt
        # unless --auto-save is explicitly supplied.
        auto_save_this_batch = (
            args.auto_save or
            (csv_expenses is not None and not args.test_first_row)
        )

        if auto_save_this_batch:
            if csv_expenses is not None and not args.test_first_row:
                print(
                    f"  Saving CSV batch {i}/{len(batches)} automatically..."
                )
            save_result = save_current_batch()
        else:
            print("\nReview the filled batch in Safari.")
            answer = input(
                "Type SAVE to save this batch, or anything else to stop: "
            ).strip()
            if answer != "SAVE":
                print("Stopped before saving.")
                return 0
            save_result = save_current_batch()

        if not save_result or not save_result.get("ok"):
            print("\nERROR: Batch was not confirmed saved.")
            print("Stopping now so the next batch is not started.")
            print("These rows have NOT been added to the local saved-state file.")
            return 6

        # Only record CSV rows in local state AFTER Expensify SAVE is confirmed.
        for item in batch:
            saved.add(expense_state_key(item))
        save_state(saved)

    print("\nDONE")
    print(f"{expense_count} expense(s) = £{total_value:.2f}")
    if csv_expenses:
        print("Processed the full CSV.")
    print("The report has NOT been submitted.")

    safari_navigate(REPORT_URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
