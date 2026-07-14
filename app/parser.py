"""Model-agnostic invoice/message parser.

Default provider is Anthropic (Claude Haiku). Switch by setting env vars:
    PARSER_PROVIDER = anthropic | openai | gemini
    PARSER_MODEL    = e.g. claude-haiku-4-5, gpt-5-mini, gemini-3-flash
No code changes needed.
"""
import base64
import json
import logging
import re

from . import config

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the data-entry brain for DOKi Foods' factory inventory system in India.
You receive either a photo of a purchase invoice / online-portal screenshot, or a short free-text
message from a factory operator. Extract a structured entry.

Entry kinds:
- "purchase": raw materials / packaging bought (updates stock AND spend)
- "expense": money spent with no stock impact (water, repairs, transport, petty cash, etc.)
- "production": finished goods produced (e.g. "produced 300 packs of chikki") — stock draw-down
- "stock_out": raw material issued/removed manually (e.g. "used 20kg flour", "threw away 5kg sugar")
- "unknown": cannot tell

Known item names (match these when possible, but include new items too): {items}
Known product names: {products}

Respond with ONLY a JSON object, no markdown fences:
{{
  "kind": "purchase|expense|production|stock_out|unknown",
  "vendor": "string or null",
  "date": "YYYY-MM-DD or null",
  "total_amount": number or null,
  "lines": [
    {{"item": "name", "qty": number, "unit": "kg|g|L|ml|pcs|box|bag", "pack_size": "e.g. 300g, 1L, null if n/a", "unit_cost": number or null, "line_total": number or null}}
  ],
  "product": "product name or null (for production)",
  "product_qty": number or null,
  "expense_category": "raw_material|packaging|utilities|repairs|transport|water|petty_cash|other or null",
  "description": "one-line human summary",
  "confidence": 0.0-1.0,
  "issues": ["anything unclear or suspicious"]
}}

Rules:
- Amounts in INR. Strip commas and currency symbols.
- Normalise units (quintal -> 100 kg, dozen -> 12 pcs).
- If an invoice has line items, extract every line.
- If handwriting/print is unreadable, lower confidence and note it in issues.
- Never invent quantities or prices.

Photo rules (real factory photos are messy):
- If MULTIPLE documents are visible, parse ONLY the front/topmost invoice and
  add an issue: "another document visible in photo — send separately if needed".
- Photos may be angled, crumpled, or partly covered by fingers — do your best
  and note anything unreadable in issues.

GST invoice rules (very common):
- "total_amount" = the final payable GRAND TOTAL (after discounts and GST),
  i.e. what will actually be paid — NOT the pre-discount item total.
- The vendor/seller is the party issuing the invoice (top-left letterhead
  usually). The buyer (often our own company) is NOT the vendor.
- Packaged goods like "MR TAMRIND CUP 300G x 72": qty=72, unit="pcs",
  pack_size="300g". Do NOT convert packs to weight.
- If line rates are pre-discount or there is an invoice-level discount/scheme,
  note it in issues (e.g. "invoice-level discount ₹1235 — effective unit cost
  lower than printed rate").
- If line totals don't reconcile with the grand total, still report the grand
  total as total_amount and note the mismatch in issues.
- The invoice date is the document's date, not today."""


def _prompt(known_items, known_products):
    return SYSTEM_PROMPT.format(
        items=", ".join(known_items) or "none yet",
        products=", ".join(known_products) or "none yet",
    )


def _extract_json(text: str) -> dict:
    """Tolerant JSON extraction — models occasionally wrap output."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ---------------------------------------------------------------- providers
def _call_anthropic(text, image_bytes, mime, system):
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    content = []
    if image_bytes:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime,
                       "data": base64.b64encode(image_bytes).decode()},
        })
    content.append({"type": "text", "text": text or "Parse this document."})
    resp = client.messages.create(
        model=config.PARSER_MODEL, max_tokens=2000, system=system,
        messages=[{"role": "user", "content": content}],
    )
    return resp.content[0].text


def _call_openai(text, image_bytes, mime, system):
    from openai import OpenAI
    client = OpenAI(api_key=config.OPENAI_API_KEY)
    content = []
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"}})
    content.append({"type": "text", "text": text or "Parse this document."})
    resp = client.chat.completions.create(
        model=config.PARSER_MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": content}],
    )
    return resp.choices[0].message.content


def _call_gemini(text, image_bytes, mime, system):
    from google import genai
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    parts = []
    if image_bytes:
        parts.append({"inline_data": {"mime_type": mime,
                                      "data": base64.b64encode(image_bytes).decode()}})
    parts.append({"text": (system + "\n\n" + (text or "Parse this document."))})
    resp = client.models.generate_content(
        model=config.PARSER_MODEL, contents=[{"role": "user", "parts": parts}],
    )
    return resp.text


_PROVIDERS = {"anthropic": _call_anthropic, "openai": _call_openai, "gemini": _call_gemini}


def parse_entry(text=None, image_bytes=None, mime="image/jpeg",
                known_items=(), known_products=()) -> dict:
    """Parse a message/photo into a structured entry dict.

    Returns the JSON structure from the system prompt. Raises on hard failure;
    caller turns that into a friendly bot message.
    """
    provider = _PROVIDERS.get(config.PARSER_PROVIDER)
    if provider is None:
        raise ValueError(f"Unknown PARSER_PROVIDER '{config.PARSER_PROVIDER}'")
    raw = provider(text, image_bytes, mime, _prompt(known_items, known_products))
    data = _extract_json(raw)
    data.setdefault("kind", "unknown")
    data.setdefault("lines", [])
    data.setdefault("issues", [])
    data.setdefault("confidence", 0.5)
    return data
