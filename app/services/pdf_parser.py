from __future__ import annotations
import re
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from pathlib import Path
import pdfplumber

logger = logging.getLogger(__name__)

class ParseError(Exception):
    pass

@dataclass
class RawTransaction:
    transaction_date: date
    raw_description:  str
    amount:           float
    page_num:         int

_DATE_FMTS = [
    "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d",
    "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y", "%d/%m/%Y",
]

def _parse_date(raw: str) -> Optional[date]:
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None

def _parse_amount(raw: str) -> Optional[float]:
    s = raw.strip().replace(",", "").replace("$", "").replace("¥", "")
    negative = s.startswith("(") or s.upper().endswith("DR")
    s = re.sub(r"[()CRDRcrdr]", "", s).strip("-").strip()
    try:
        val = float(s)
        return -abs(val) if negative else val
    except ValueError:
        return None

_COL_MAP = {
    r"(trans|posting|transaction)\s*(date|dt)": "date",
    r"(date)": "date",
    r"(description|memo|details|narration)": "desc",
    r"(debit|withdrawal|charge)": "debit",
    r"(credit|deposit|payment)": "credit",
    r"(amount)": "amount",
}

def _map_headers(headers):
    mapping = {}
    for i, h in enumerate(headers or []):
        if not h:
            continue
        h_clean = str(h).strip().lower()
        for pattern, canon in _COL_MAP.items():
            if re.search(pattern, h_clean) and canon not in mapping.values():
                mapping[i] = canon
                break
    return mapping

def _rows_from_table(table, page_num):
    if not table or len(table) < 2:
        return []
    headers = [str(c).strip() if c else "" for c in table[0]]
    col_map = _map_headers(headers)
    if "date" not in col_map.values() or "desc" not in col_map.values():
        return []
    results = []
    for row in table[1:]:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue
        cells = [str(c).strip() if c is not None else "" for c in row]
        date_idx = next((k for k, v in col_map.items() if v == "date"), None)
        txn_date = _parse_date(cells[date_idx]) if date_idx is not None and date_idx < len(cells) else None
        if not txn_date:
            continue
        desc_idx = next((k for k, v in col_map.items() if v == "desc"), None)
        desc = cells[desc_idx] if desc_idx is not None and desc_idx < len(cells) else ""
        if not desc:
            continue
        amount = None
        if "debit" in col_map.values() and "credit" in col_map.values():
            di = next(k for k, v in col_map.items() if v == "debit")
            ci = next(k for k, v in col_map.items() if v == "credit")
            dv = _parse_amount(cells[di]) if di < len(cells) else None
            cv = _parse_amount(cells[ci]) if ci < len(cells) else None
            if dv: amount = -abs(dv)
            elif cv: amount = abs(cv)
        else:
            ai = next((k for k, v in col_map.items() if v == "amount"), None)
            if ai is not None and ai < len(cells):
                amount = _parse_amount(cells[ai])
        if amount is None:
            continue
        results.append(RawTransaction(transaction_date=txn_date, raw_description=desc, amount=amount, page_num=page_num))
    return results

_LINE_RE = re.compile(
    r"(?P<date>\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}[\/\-]\d{2}[\/\-]\d{2})"
    r"\s+(?P<desc>[A-Za-z0-9*#@&.,\-\s]{5,60}?)\s+"
    r"(?P<amount>-?\$?[\d,]+\.\d{2})"
)

def _rows_from_text(text, page_num):
    results = []
    for line in text.splitlines():
        m = _LINE_RE.search(line)
        if not m:
            continue
        txn_date = _parse_date(m.group("date"))
        amount = _parse_amount(m.group("amount"))
        if txn_date and amount is not None:
            results.append(RawTransaction(
                transaction_date=txn_date,
                raw_description=m.group("desc").strip(),
                amount=amount,
                page_num=page_num,
            ))
    return results

def parse_pdf(path) -> list[RawTransaction]:
    path = Path(path)
    if not path.exists():
        raise ParseError(f"File not found: {path}")
    all_rows = []
    with pdfplumber.open(str(path)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables({"vertical_strategy":"lines","horizontal_strategy":"lines","snap_tolerance":3})
            table_rows = []
            for table in (tables or []):
                table_rows.extend(_rows_from_table(table, page_num))
            if table_rows:
                all_rows.extend(table_rows)
                continue
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            all_rows.extend(_rows_from_text(text, page_num))
    if not all_rows:
        raise ParseError("Could not extract transactions. Please ensure this is a digital (not scanned) bank statement.")
    seen = set()
    unique = []
    for row in all_rows:
        key = (row.transaction_date, row.raw_description[:40], row.amount)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    logger.info("Parsed %d unique transactions from %s", len(unique), path.name)
    return unique