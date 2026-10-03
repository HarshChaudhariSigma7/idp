"""Structured-output compiler limits (platform docs): <=16 union-typed params, <=24 optional params,
additionalProperties:false everywhere. These tests keep every schema inside them."""
import json

from sereno.extraction.crossread import CROP_SCHEMA
from sereno.extraction.doc_specs import SPECS, TRIAGE_SCHEMA, primary_schema, spec_with_extras


def _walk(node, stats):
    if isinstance(node, dict):
        if "anyOf" in node or isinstance(node.get("type"), list):
            stats["unions"] += 1
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, node
            props = set(node.get("properties", {}))
            stats["optional"] += len(props - set(node.get("required", [])))
        for k in ("minimum", "maximum", "minLength", "maxLength", "pattern"):
            assert k not in node, f"unsupported constraint {k}"
        for v in node.values():
            _walk(v, stats)
    elif isinstance(node, list):
        for v in node:
            _walk(v, stats)


def test_all_schemas_within_limits():
    schemas = [TRIAGE_SCHEMA, CROP_SCHEMA]
    for t in SPECS:
        schemas.append(primary_schema(SPECS[t]))
    schemas.append(primary_schema(spec_with_extras("invoice", [{"name": "vendor_code", "label": "Vendor code"}])))
    for sch in schemas:
        stats = {"unions": 0, "optional": 0}
        _walk(sch, stats)
        assert stats["unions"] == 0 and stats["optional"] == 0, stats
        assert len(json.dumps(sch)) < 20000


def test_extra_fields_are_sanitised():
    spec = spec_with_extras("invoice", [{"name": "Robert'); DROP", "label": "x"}, {"name": "grand_total"},
                                        {"name": "batch_no", "label": "Batch no.", "type": "weird"}])
    names = [f.name for f in spec.fields]
    assert "batch_no" in names and names.count("grand_total") == 1 and not any("DROP" in n for n in names)
