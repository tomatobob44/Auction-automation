"""Minimal phone-openable web UI (FastAPI).

A single mobile-friendly page: type a UPC/ASIN/title OR upload a barcode photo,
set condition / bin cost / weight / battery, optionally paste Terapeak
median+count, and get a terse BUY/SKIP verdict. Plus an outcome-recording form.

Bind to 127.0.0.1 by default (see cli serve --host). There is no auth — only
expose it on a trusted LAN.

The app factory takes an optional `sources_factory` so tests can inject a
FakeCompSource without any API key.
"""
from __future__ import annotations

import html
from typing import Callable

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from .comps import CompSource, ManualCompSource
from .config import Settings, load_settings
from .db import connect, record_outcome
from .engine import evaluate
from .models import Condition, WeightClass
from .verdict import format_terse

SourcesFactory = Callable[[Settings, Condition, float | None, int | None], list[CompSource]]


def _default_sources_factory(settings, condition, manual_median, manual_count):
    sources: list[CompSource] = []
    if settings.keepa_api_key:
        from .keepa import KeepaSource
        sources.append(KeepaSource(settings.keepa_api_key))
    if manual_median is not None:
        sources.append(ManualCompSource(
            median=manual_median, count=manual_count or 0, condition=condition))
    return sources


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>binval</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 1rem;
          background: #111; color: #eee; }}
  h1 {{ font-size: 1.2rem; }}
  form {{ margin-bottom: 1.5rem; }}
  label {{ display:block; margin:.5rem 0 .15rem; font-size:.85rem; color:#aaa; }}
  input, select {{ width:100%; padding:.6rem; font-size:1rem; box-sizing:border-box;
            background:#222; color:#eee; border:1px solid #444; border-radius:6px; }}
  .row {{ display:flex; gap:.5rem; }} .row > div {{ flex:1; }}
  .check {{ display:flex; align-items:center; gap:.5rem; margin-top:.6rem; }}
  .check input {{ width:auto; }}
  button {{ margin-top:1rem; width:100%; padding:.9rem; font-size:1.1rem;
            background:#2d7; color:#012; border:none; border-radius:8px; font-weight:700; }}
  .verdict {{ padding:1rem; border-radius:8px; font-size:1.3rem; font-weight:700;
              white-space:pre-wrap; word-break:break-word; }}
  .buy {{ background:#0a3; color:#fff; }} .skip {{ background:#a30; color:#fff; }}
  hr {{ border:0; border-top:1px solid #333; margin:1.5rem 0; }}
  small {{ color:#888; }}
</style></head>
<body>
<h1>binval — aisle verdict</h1>
{verdict_block}
<form method="post" action="/eval" enctype="multipart/form-data">
  <label>Identifier (UPC / ASIN / title)</label>
  <input name="identifier" placeholder="012345678905" value="">
  <label>…or barcode photo</label>
  <input type="file" name="image" accept="image/*" capture="environment">
  <div class="row">
    <div><label>Bin cost $</label><input name="bin_cost" type="number" step="0.01" required></div>
    <div><label>Condition</label><select name="condition">{cond_opts}</select></div>
  </div>
  <div class="row">
    <div><label>Weight</label><select name="weight">{weight_opts}</select></div>
    <div><label>Marketplace</label><select name="marketplace">
      <option value="ebay">eBay</option><option value="amazon">Amazon</option></select></div>
  </div>
  <div class="row">
    <div><label>Terapeak median $ (optional)</label><input name="manual_median" type="number" step="0.01"></div>
    <div><label>Terapeak sold count</label><input name="manual_count" type="number"></div>
  </div>
  <div class="check"><input type="checkbox" name="lithium" value="1"><label style="margin:0">Lithium battery</label></div>
  <button type="submit">VALUE IT</button>
</form>
<hr>
<form method="post" action="/outcome">
  <small>Record a sale outcome</small>
  <div class="row">
    <div><label>Scan #</label><input name="scan_id" type="number" required></div>
    <div><label>Sold $</label><input name="sold" type="number" step="0.01" required></div>
  </div>
  <div class="row">
    <div><label>Ship $</label><input name="ship" type="number" step="0.01" required></div>
    <div><label>Days to sell</label><input name="days" type="number"></div>
  </div>
  <label>Labor minutes (list + pack + ship)</label>
  <input name="minutes" type="number" step="1">
  <div class="check"><input type="checkbox" name="returned" value="1"><label style="margin:0">Returned</label></div>
  <label>Return reason</label><input name="reason" placeholder="INAD / DOA">
  <button type="submit" style="background:#46c">RECORD OUTCOME</button>
</form>
</body></html>
"""


def _render(verdict_html: str = "") -> str:
    cond_opts = "".join(
        f'<option value="{c.value}"{" selected" if c == Condition.OPEN_BOX else ""}>'
        f'{c.value}</option>' for c in Condition
    )
    weight_opts = "".join(
        f'<option value="{w.value}"{" selected" if w == WeightClass.LB_1_3 else ""}>'
        f'{w.value}</option>' for w in WeightClass
    )
    return _PAGE.format(verdict_block=verdict_html, cond_opts=cond_opts,
                        weight_opts=weight_opts)


def _verdict_block(line: str, decision: str) -> str:
    cls = "buy" if decision == "BUY" else "skip"
    return f'<div class="verdict {cls}">{html.escape(line)}</div>'


def create_app(settings: Settings | None = None,
               sources_factory: SourcesFactory | None = None) -> FastAPI:
    settings = settings or load_settings()
    sources_factory = sources_factory or _default_sources_factory
    app = FastAPI(title="binval")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _render()

    @app.post("/eval", response_class=HTMLResponse)
    async def eval_item(
        identifier: str = Form(""),
        bin_cost: float = Form(...),
        condition: str = Form(Condition.OPEN_BOX.value),
        weight: str = Form(WeightClass.LB_1_3.value),
        marketplace: str = Form("ebay"),
        manual_median: str = Form(""),
        manual_count: str = Form(""),
        lithium: str = Form(""),
        image: UploadFile | None = None,
    ) -> str:
        cond = Condition(condition)
        m_median = float(manual_median) if manual_median.strip() else None
        m_count = int(manual_count) if manual_count.strip() else None
        sources = sources_factory(settings, cond, m_median, m_count)
        if not sources:
            return _render(_verdict_block(
                "No comp sources. Set KEEPA_API_KEY or enter Terapeak values.",
                "SKIP"))

        if image is not None and image.filename:
            raw: str | bytes = await image.read()
        else:
            raw = identifier

        conn = connect(settings.db_path)
        verdict, comps, costs, scan_id = evaluate(
            raw, bin_cost=bin_cost, condition=cond,
            weight_class=WeightClass(weight), marketplace=marketplace,
            lithium=bool(lithium), sources=sources, settings=settings, conn=conn,
        )
        line = format_terse(verdict, comps, scan_id)
        return _render(_verdict_block(line, verdict.decision))

    @app.post("/outcome", response_class=HTMLResponse)
    def outcome(
        scan_id: int = Form(...),
        sold: float = Form(...),
        ship: float = Form(...),
        days: str = Form(""),
        minutes: str = Form(""),
        returned: str = Form(""),
        reason: str = Form(""),
    ) -> str:
        conn = connect(settings.db_path)
        actual = record_outcome(
            conn, scan_id, sold_price=sold, ship_cost=ship,
            tables=settings.costs, was_returned=bool(returned),
            return_reason=reason or None,
            days_to_sell=int(days) if days.strip() else None,
            labor_minutes=float(minutes) if minutes.strip() else None,
        )
        msg = f"#{scan_id} actual_net ${actual:+.2f}"
        if returned:
            msg += "  [RETURNED]"
        return _render(_verdict_block(msg, "BUY" if actual >= 0 else "SKIP"))

    return app
