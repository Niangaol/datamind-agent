"""Built-in parser plugin for native DXF drawings.

The declaration claims the v2 capability set of ``ingestion.cad_adapter``, because the
adapter's default profile is v2. DWG is deliberately absent: it needs an external
converter and is refused with ``CAD_DWG_CONVERTER_UNAVAILABLE`` before any adapter runs
(G5), so declaring it here would advertise a parse that cannot happen.

The upload path additionally carried a ``provenance`` hint and retried without it when the
adapter's handler did not accept the keyword; that fallback lives here now, so routing the
registry in front of the upload path cannot drop it. Importing this module costs nothing
heavy (G7): ``ingestion.cad_adapter``, ``ezdxf`` and ``fastapi`` are imported inside
:func:`parse` only.
"""
from __future__ import annotations

from datamind.plugin.manifest import ParserPluginManifest

MANIFEST = ParserPluginManifest(
    'dxf-native', '1.0.0', ('image/vnd.dxf',), ('.dxf',),
    capabilities=('entities', 'text', 'layers', 'dimensions', 'blocks', 'arcs',
                  'polylines', 'mtext', 'native_dimensions'),
    loader='ingestion.plugins.dxf_plugin:parse')


def parse(path, document_id, source_version, options=None):
    """Parse one DXF and summarise its layers, entities and dimensions.

    The summary counts only ``cad_entity`` blocks so a block-level locator from another
    adapter could never be misread as a drawing layer, exactly as the upload path did.
    """
    from fastapi import HTTPException

    from ingestion import cad_adapter

    provenance = (options or {}).get('provenance')
    try:
        result = cad_adapter.parse(path, document_id=document_id,
                                   source_version=source_version, provenance=provenance)
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        # Kept from the upload path: a future profile revision may stop accepting
        # `provenance`, and the documented entry point is the two-keyword form.
        if (detail.get('details') or {}).get('reason') != 'handler_signature':
            raise
        result = cad_adapter.parse(path, document_id=document_id,
                                   source_version=source_version)
    payload = result.payload
    layers = sorted({block['locator'].get('layer') for block in payload['blocks']
                     if isinstance(block.get('locator'), dict)
                     and block['locator'].get('locator_kind') == 'cad_entity'
                     and block['locator'].get('layer')})
    entities = sum(1 for block in payload['blocks'] if block['block_type'] != 'dimension')
    dimensions = sum(1 for block in payload['blocks'] if block['block_type'] == 'dimension')
    return result, {'layers': len(layers), 'layer_names': layers, 'entities': entities,
                    'dimensions': dimensions}
