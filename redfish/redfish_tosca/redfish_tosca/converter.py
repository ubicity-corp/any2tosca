class RedfishToToscaConverter:
    TYPE_MAP = {
        "string": "string",
        "integer": "integer",
        "number": "float",
        "boolean": "boolean",
        "array": "list",
        "object": "map",
    }

    def convert(self, schema: dict) -> dict:
        title = schema.get("title", "RedfishNode")
        node_name = f"redfish.nodes.{title}"

        return {
            node_name: {
                "derived_from": "tosca.nodes.Root",
                "properties": self._convert_properties(schema),
            }
        }

    def _convert_properties(self, schema):
        props = {}
        required = set(schema.get("required", []))

        for name, prop in schema.get("properties", {}).items():
            mapped = {
                "type": self.TYPE_MAP.get(prop.get("type"), "string")
            }
            if name in required:
                mapped["required"] = True

            props[name] = mapped

        return props