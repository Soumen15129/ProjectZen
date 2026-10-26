"""
test_input_fit.py — pure tests for input_fit.py (no LLM, no files).

    python tests/test_input_fit.py
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from input_fit import trim_low_value_sheets, fair_shares, cut_to, fit, LOW_VALUE_SHEETS

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def workbook(*sheets):
    """Exactly extractor.py's format: '[Name]\\n' + rows, sheets joined by a blank line."""
    return "\n\n".join(f"[{name}]\n" + "\n".join(rows) for name, rows in sheets)


print("\n1. Sheet trimming")
wb = workbook(("Cover Sheet", []), ("TOC", ["Contents,Page"]),
              ("Translations", ["Budget,Presupuesto,预算"] * 50),
              ("Design Worksheet", ["Field,Type", "salary,Amount"]),
              ("Translation Rules", ["rule one"]))
out, dropped = trim_low_value_sheets(wb)
names = [n for n, _ in dropped]
check("drops Cover Sheet, TOC, Translations", names == ["Cover Sheet", "TOC", "Translations"], names)
check("keeps the real content sheet", "[Design Worksheet]\nField,Type\nsalary,Amount" in out)
check("keeps 'Translation Rules' — whole-name match only", "[Translation Rules]\nrule one" in out)
check("reports dropped sizes", all(size > 0 for _, size in dropped))

plain = workbook(("Config", ["a,b"]), ("Empty", []), ("More", ["c,d"]))
check("nothing to trim -> byte-identical", trim_low_value_sheets(plain) == (plain, []))
only_low = workbook(("Translations", ["x"]), ("TOC", ["y"]))
check("never empties a file", trim_low_value_sheets(only_low) == (only_low, []))
check("non-spreadsheet text untouched",
      trim_low_value_sheets("SOW text\n\n[TBC] item") == ("SOW text\n\n[TBC] item", []))
check("names are case-insensitive", "translations" in LOW_VALUE_SHEETS
      and trim_low_value_sheets(workbook(("TRANSLATIONS", ["x"]), ("A", ["y"])))[1] != [])
first = workbook(("Translations", ["x"]), ("Config", ["a,b"]))
check("dropping the FIRST sheet keeps the next one's header",
      trim_low_value_sheets(first)[0] == "[Config]\na,b")

print("\n2. Fair shares")
check("everything fits -> unchanged", fair_shares([10, 20], 100) == [10, 20])
s = fair_shares([11_000, 121_000, 182_000], 300_000)
check(f"small file kept whole, large ones share: {s}", s == [11_000, 121_000, 168_000])
s = fair_shares([150_000, 150_000, 150_000], 300_000)
check(f"equal files split equally: {s}", s == [100_000, 100_000, 100_000])
s = fair_shares([5, 500_000], 300_000)
check("tiny file is never squeezed", s[0] == 5)
check("never exceeds the budget", sum(fair_shares([90_000] * 7, 300_000)) <= 300_000)

print("\n3. Cutting")
text = "\n".join(f"row {i:05d} some content here" for i in range(2_000))
c = cut_to(text, 10_000)
check("result fits the limit", len(c) <= 10_000, len(c))
check("ends with a continuation marker", c.endswith("more characters of this file not shown ...]"))
check("cut on a line boundary", c.split("\n[...")[0].endswith("some content here"))
check("short text unchanged", cut_to("abc", 10) == "abc")

print("\n4. Fit")
parts = [{"name": "rtm", "text": "r\n" * 60_000}, {"name": "tmpl", "text": "t\n" * 5_000},
         {"name": "wb", "text": "w\n" * 150_000}]
cut, rows = fit(parts, overhead=1_000, window=200_000)
total = sum(len(p["text"]) for p in cut) + 1_000
check(f"fitted total within the window ({total:,})", total <= 200_000)
check("small file kept whole", rows[1]["kept"] == rows[1]["chars"])
check("every file contributes", all(r["kept"] > 0 for r in rows))
check("names and order preserved", [r["name"] for r in rows] == ["rtm", "tmpl", "wb"])
same, rows = fit(parts[:1], overhead=0, window=1_000_000)
check("fits already -> untouched", same[0]["text"] == parts[0]["text"]
      and rows[0]["kept"] == rows[0]["chars"])

print("\nRESULT:", "PASS" if not FAILS else f"FAIL ({len(FAILS)}): {FAILS}")
sys.exit(1 if FAILS else 0)
