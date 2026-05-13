"""Shared utilities — Ceraldi Group ERP"""
import uuid
from bson import ObjectId


def serialize_doc(doc: dict) -> dict:
    """Convert ObjectId fields to strings; handles nested dicts and lists."""
    if not doc:
        return {}
    out = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, list):
            out[k] = [serialize_doc(i) if isinstance(i, dict) else
                      str(i) if isinstance(i, ObjectId) else i for i in v]
        elif isinstance(v, dict):
            out[k] = serialize_doc(v)
        else:
            out[k] = v
    return out


def new_id() -> str:
    return str(uuid.uuid4())
