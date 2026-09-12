"""
parsers/ — structured input parsers for adhoc quality improvement.

Item 06: xlsx_config.parse_xlsx_extraction()
  Parses the tab-CSV text that extractor.py produces from uploaded xlsx files
  and converts it to a compact, field-level summary.  Claude reads exact field
  IDs, types, and picklist values instead of inferring them from free-form text.

ADHOC ONLY — these parsers are called only from adhoc_pipeline.py.
Cascade uses its own source-text pipeline and must not be affected.
"""
from parsers.xlsx_config import parse_xlsx_extraction

__all__ = ["parse_xlsx_extraction"]
