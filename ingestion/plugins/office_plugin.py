"""Built-in parser plugin declarations for the three OOXML formats.

Each format gets its own manifest because each is a separate media type with its own
extension and capability set, but all three share one loader: ``ingestion.office.parse``
re-sniffs the package and dispatches to whichever ``*-native`` adapter matches, so the
plugin layer only needs to forward, never to choose. The declarations mirror the names,
media types and capability lists ``ingestion.office.register_office_adapters`` already
registers, so the adapter map and the plugin inventory cannot drift apart.

Importing this module costs nothing heavy (G7): ``ingestion.office`` (and through it
``python-docx``/``openpyxl``/``python-pptx``) is imported inside :func:`parse` only.
"""
from __future__ import annotations

from datamind.plugin.manifest import ParserPluginManifest

_LOADER = 'ingestion.plugins.office_plugin:parse'
_DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
_PPTX = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

DOCX_MANIFEST = ParserPluginManifest(
    'docx-native', '1.0.0', (_DOCX,), ('.docx',),
    capabilities=('text', 'headings', 'tables'), loader=_LOADER)
PPTX_MANIFEST = ParserPluginManifest(
    'pptx-native', '1.0.0', (_PPTX,), ('.pptx',),
    capabilities=('text', 'slides'), loader=_LOADER)
XLSX_MANIFEST = ParserPluginManifest(
    'xlsx-native', '1.0.0', (_XLSX,), ('.xlsx',),
    capabilities=('text', 'sheet_rows', 'cached_values'), loader=_LOADER)

MANIFESTS = (DOCX_MANIFEST, PPTX_MANIFEST, XLSX_MANIFEST)


def parse(path, document_id, source_version, options=None):
    """Parse one Office file through the unchanged ``ingestion.office.parse`` entry point.

    ``options`` is part of the uniform plugin signature but is deliberately not forwarded:
    the upload path has always parsed with the adapter defaults, and passing the claim
    through would change which reader options the Office handlers receive.
    """
    from ingestion import office

    result = office.parse(path, document_id=document_id, source_version=source_version)
    return result, {}
