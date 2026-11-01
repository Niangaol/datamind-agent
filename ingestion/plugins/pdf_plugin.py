"""Built-in parser plugin for PDFs, wrapping the existing ``pdf-native`` adapter.

The declaration names the adapter that ``ingestion.ingest`` already registers
(``_PDF_ADAPTER``) and claims the same capability set, so the plugin inventory and the
contract adapter cannot drift apart. ``loader`` is a ``"module:function"`` reference
(G7): importing this module costs nothing heavy, and ``pymupdf``, ``ingestion.ingest`` and
``datamind.parse_contract`` are imported inside :func:`parse` only.
"""
from __future__ import annotations

from datamind.plugin.manifest import ParserPluginManifest

MANIFEST = ParserPluginManifest(
    'pdf-native', '1.0.0', ('application/pdf',), ('.pdf',),
    capabilities=('native_text', 'pixel_ocr', 'tables', 'formula'),
    loader='ingestion.plugins.pdf_plugin:parse')


def parse(path, document_id, source_version, options=None):
    """Parse one PDF and dispatch it through the registered ``application/pdf`` adapter.

    Byte-for-byte the composition ``upload_pipeline._parse_pdf`` used to inline: the
    ingestion parser produces the blocks and quality detail, the page sizes come from the
    same file, and the contract validates the adapter's mapping. ``source_version`` and
    ``options`` are part of the uniform plugin signature; the PDF path reads neither,
    because the quality detail it carries already holds its own ``source_sha256``.
    """
    from datamind import parse_contract

    from ingestion import ingest

    blocks, page_count, detail = ingest.parse_pdf(path, document_id, include_quality=True)
    import pymupdf
    document = pymupdf.open(path)
    try:
        page_sizes = {index + 1: (float(page.rect.width), float(page.rect.height))
                      for index, page in enumerate(document)}
    finally:
        document.close()
    ingest._ensure_pdf_adapter()
    result = parse_contract.dispatch('application/pdf', pdf_path=path,
                                     doc_id=document_id, blocks=blocks,
                                     page_sizes=page_sizes, detail=detail,
                                     parser_version=ingest.PARSER_VERSION)
    return result, {'pages': page_count}
