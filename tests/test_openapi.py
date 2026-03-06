"""
tests/test_openapi.py
---------------------
Tests for the OpenAPIExtractor pre-processor.
"""

import pytest
from any2tosca.openapi import OpenAPIExtractor
from any2tosca.converter import Converter

e = OpenAPIExtractor()

# ── 3.x schema extraction ─────────────────────────────────────────────────────

OA3_DOC = {
    "openapi": "3.0.0",
    "components": {
        "schemas": {
            "Server": {
                "type": "object",
                "properties": {
                    "id":   {"type": "string"},
                    "cpu":  {"type": "integer"},
                },
            },
            "Error": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
        }
    },
    "paths": {
        "/servers": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Server"}
                        }
                    }
                }
            }
        }
    },
}


def test_3x_definitions_extracted():
    root, _ = e.extract(OA3_DOC)
    assert "Server" in root["definitions"]
    assert "Error" in root["definitions"]


def test_3x_refs_rewritten():
    doc = {
        "openapi": "3.0.0",
        "components": {
            "schemas": {
                "Container": {
                    "type": "object",
                    "properties": {
                        "item": {"$ref": "#/components/schemas/Item"}
                    },
                },
                "Item": {"type": "object", "properties": {"name": {"type": "string"}}},
            }
        },
        "paths": {},
    }
    root, _ = e.extract(doc)
    assert root["definitions"]["Container"]["properties"]["item"]["$ref"] == "#/definitions/Item"


def test_3x_post_schema_becomes_node_type():
    _, node_type_names = e.extract(OA3_DOC)
    assert "Server" in node_type_names
    assert "Error" not in node_type_names


def test_3x_no_post_means_no_node_types():
    doc = {**OA3_DOC, "paths": {"/servers": {"get": {}}}}
    _, node_type_names = e.extract(doc)
    assert len(node_type_names) == 0


# ── nullable normalisation ────────────────────────────────────────────────────

def test_nullable_true_dropped():
    doc = {
        "openapi": "3.0.0",
        "components": {"schemas": {"Foo": {"type": "string", "nullable": True}}},
        "paths": {},
    }
    root, _ = e.extract(doc)
    assert "nullable" not in root["definitions"]["Foo"]


def test_anyof_null_flattened():
    doc = {
        "openapi": "3.0.0",
        "components": {
            "schemas": {
                "Foo": {
                    "type": "object",
                    "properties": {
                        "val": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        }
                    },
                }
            }
        },
        "paths": {},
    }
    root, _ = e.extract(doc)
    assert root["definitions"]["Foo"]["properties"]["val"] == {"type": "string"}


# ── OpenAPI 2.0 ───────────────────────────────────────────────────────────────

OA2_DOC = {
    "swagger": "2.0",
    "definitions": {
        "Widget": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
        },
        "Error": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
        },
    },
    "paths": {
        "/widgets": {
            "post": {
                "parameters": [
                    {
                        "in": "body",
                        "name": "body",
                        "schema": {"$ref": "#/definitions/Widget"},
                    }
                ]
            }
        }
    },
}


def test_2x_definitions_extracted():
    root, _ = e.extract(OA2_DOC)
    assert "Widget" in root["definitions"]
    assert "Error" in root["definitions"]


def test_2x_post_schema_becomes_node_type():
    _, node_type_names = e.extract(OA2_DOC)
    assert "Widget" in node_type_names
    assert "Error" not in node_type_names


# ── End-to-end with Converter ─────────────────────────────────────────────────

def test_end_to_end_3x():
    root, node_type_names = e.extract(OA3_DOC)
    result = Converter().convert_schema(root, "api", node_type_names)
    assert "Server" in result.node_types
    assert "derived_from" not in result.node_types["Server"]
    assert "Error" in result.data_types


def test_end_to_end_2x():
    root, node_type_names = e.extract(OA2_DOC)
    result = Converter().convert_schema(root, "api", node_type_names)
    assert "Widget" in result.node_types
    assert "Error" in result.data_types
