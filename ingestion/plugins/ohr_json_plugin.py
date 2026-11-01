"""Built-in declaration for the OHR-Bench JSON contract path.

There is no file parser behind this plugin. An OHR-Bench record has a page index and GT
text but no bbox and no page size, so the existing handler
(``ingestion.ingest._ohr_contract_summary``) routes it through the contract as an explicit
"no locator" gap. JSON has no signature to sniff, which is why dispatch here is explicit by
media type and why the manifest declares no extension at all (G11): claiming ``.json``
would steal the measurement-JSON branch from upload routing.
"""
from __future__ import annotations

from datamind.plugin.manifest import ParserPluginManifest

MANIFEST = ParserPluginManifest(
    'ohr-bench-json', '1.0.0', ('application/vnd.ohr-bench+json',),
    capabilities=('native_text',),
    loader='ingestion.ingest:_ohr_contract_summary')
