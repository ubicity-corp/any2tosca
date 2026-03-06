import json
import yaml
from pathlib import Path
import argparse
from referencing import Registry, Resource

TYPE_MAP = {
    "string": "string",
    "integer": "integer",
    "number": "float",
    "boolean": "boolean",
    "array": "list",
    "object": "map",
}

def resolve_schema(schema_file: Path, registry=None):
    schema = json.loads(schema_file.read_text())
    if registry is None:
        registry = Registry()
    uri = schema.get("$id") or schema_file.resolve().as_uri()
    registry = registry.with_resource(uri, Resource.from_contents(schema))
    resource = Resource.from_contents(schema)
    resolver = registry.resolver_with_root(resource)

    def _resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref_node = resolver.lookup(node["$ref"]).contents
                return _resolve(ref_node)
            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(i) for i in node]
        return node

    resolved_schema = _resolve(schema)
    return resolved_schema, registry

def convert_to_tosca(schema: dict):
    tosca = {"data_types": {schema.get("title","MyDataType"): {"properties": {}}}}
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    for name, prop in props.items():
        if name == "Links":
            links_map = {k: {"type": "list", "entry_schema": {"type": "string"}} for k in prop.get("properties", {})}
            tosca["data_types"][schema.get("title")]["properties"]["Links"] = {"type": "map", "properties": links_map}
            continue
        tosca_prop = {"type": TYPE_MAP.get(prop.get("type"), "string")}
        if name in required:
            tosca_prop["required"] = True
        if "enum" in prop:
            tosca_prop["constraints"] = [{"valid_values": prop["enum"]}]
        tosca["data_types"][schema.get("title")]["properties"][name] = tosca_prop
    return tosca

def main():
    parser = argparse.ArgumentParser(description="Convert JSON Schema to TOSCA Data Types (supports $ref)")
    parser.add_argument("input", type=Path, help="Input JSON Schema file")
    parser.add_argument("output", type=Path, help="Output YAML file")
    args = parser.parse_args()

    resolved_schema, _ = resolve_schema(args.input)
    tosca = convert_to_tosca(resolved_schema)
    args.output.write_text(yaml.safe_dump(tosca, sort_keys=False))
    print(f"Wrote TOSCA data type to {args.output}")

if __name__ == "__main__":
    main()