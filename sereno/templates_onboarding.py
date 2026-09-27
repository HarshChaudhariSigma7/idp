""""Show me 3-5 examples" template onboarding. From reviewed examples of one vendor's format we
derive layout hints (where each field sits, what it looks like, what reviewers had to fix) that
are injected into the extraction prompt for that vendor. Deterministic, no extra model call, no
code change. Customers can also add extra fields for a template."""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict

from sqlalchemy import select

from sereno.models import Document, Template
from sereno.review import effective_value
from sereno.security import audit

MIN_EXAMPLES, MAX_EXAMPLES = 3, 5


class TemplateError(ValueError):
    pass


def _mask(s: str) -> str:
    """Shape of a value without its content: INV/24-25/0123 -> AAA/99-99/9999."""
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", s))[:40]


def _where(x: float, y: float) -> str:
    v = "top" if y < 0.33 else ("middle" if y < 0.66 else "bottom")
    h = "left" if x < 0.33 else ("centre" if x < 0.66 else "right")
    return f"{v} {h}"


def build_hints(docs: list[Document]) -> str:
    loc = defaultdict(list)
    shapes = defaultdict(Counter)
    corrected = Counter()
    labels = {}
    for d in docs:
        for f in d.fields:
            if f.line_index is not None:
                if f.status == "corrected":
                    corrected[f"line item {f.field_name}"] += 1
                continue
            labels[f.field_name] = f.label
            v = effective_value(f)
            if v is None:
                continue
            if f.bbox and f.page:
                loc[f.field_name].append((f.page, (f.bbox["x0"] + f.bbox["x1"]) / 2, (f.bbox["y0"] + f.bbox["y1"]) / 2))
            if f.value_type in ("string", "gstin", "vehicle", "hsn") and isinstance(v, str):
                shapes[f.field_name][_mask(v)] += 1
            if f.status == "corrected":
                corrected[f.field_name] += 1
    out = []
    for name, pts in sorted(loc.items()):
        if len(pts) < 2:
            continue
        pages = Counter(p for p, _, _ in pts).most_common(1)[0][0]
        x, y = statistics.median(p[1] for p in pts), statistics.median(p[2] for p in pts)
        spread = max(statistics.pstdev([p[2] for p in pts]), statistics.pstdev([p[1] for p in pts]))
        if spread > 0.12:
            continue  # position not stable across examples; no hint
        line = f"- {name} ({labels.get(name, name)}): page {pages}, {_where(x, y)} (x≈{x:.2f}, y≈{y:.2f})"
        if shapes[name]:
            mask, n = shapes[name].most_common(1)[0]
            if n >= 2:
                line += f"; usually formatted like {mask} (A=letter, 9=digit)"
        out.append(line)
    n = len(docs)
    for name, c in corrected.most_common():
        out.append(f"- Reviewers corrected {name} in {c} of {n} examples: read it especially carefully.")
    return "\n".join(out)


def create_template(s, user, doc_type: str, name: str, example_ids: list[str], extra_fields: list[dict] | None) -> Template:
    ids = list(dict.fromkeys(example_ids))
    if not (MIN_EXAMPLES <= len(ids) <= MAX_EXAMPLES):
        raise TemplateError(f"Pick {MIN_EXAMPLES} to {MAX_EXAMPLES} example documents")
    docs = s.execute(select(Document).where(Document.id.in_(ids), Document.tenant_id == user.tenant_id)).scalars().all()
    if len(docs) != len(ids):
        raise TemplateError("Some examples were not found")
    if any(d.doc_type != doc_type for d in docs):
        raise TemplateError("All examples must be the same document type")
    if any(d.status not in ("ready", "exported") for d in docs):
        raise TemplateError("Examples must be fully processed and reviewed first")
    vendors = {d.vendor_key for d in docs if d.vendor_key}
    if len(vendors) > 1:
        raise TemplateError("Examples come from different vendors; a template is for one vendor's format")
    t = Template(tenant_id=user.tenant_id, doc_type=doc_type, name=name[:200], vendor_key=next(iter(vendors), None),
                 hints=build_hints(docs), extra_fields=extra_fields or [], example_document_ids=ids,
                 status="draft", created_by=user.id)
    s.add(t)
    s.flush()
    audit.record("template.created", session=s, tenant_id=user.tenant_id, actor_id=user.id, object_type="template", object_id=t.id,
                 examples=len(ids), doc_type=doc_type, extra_fields=len(extra_fields or []))
    return t


def activate(s, user, t: Template) -> None:
    if not t.vendor_key:
        raise TemplateError("Couldn't identify the vendor (no GSTIN) on these examples")
    for other in s.execute(select(Template).where(Template.tenant_id == t.tenant_id, Template.vendor_key == t.vendor_key,
                                                  Template.doc_type == t.doc_type, Template.status == "active")).scalars():
        other.status = "retired"
    t.status = "active"
    audit.record("template.activated", session=s, tenant_id=t.tenant_id, actor_id=user.id, object_type="template", object_id=t.id)
