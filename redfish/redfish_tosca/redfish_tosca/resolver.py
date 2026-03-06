from referencing import Resource

def dereference(schema: dict, registry):
    resource = Resource.from_contents(schema)
    resolver = registry.resolver_with_root(resource)

    def _resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                resolved = resolver.lookup(node["$ref"]).contents
                return _resolve(resolved)
            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(i) for i in node]
        return node

    return _resolve(schema)