def flatten_allOf(schema: dict):
    props = {}

    for item in schema.get("allOf", []):
        props.update(flatten_allOf(item))

    props.update(schema.get("properties", {}))
    return props