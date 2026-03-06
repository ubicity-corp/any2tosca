from pathlib import Path

from .loader import SchemaLoader
from .resolver import dereference
from .flattener import flatten_allOf
from .converter import RedfishToToscaConverter
from .writer import ToscaWriter

def generate(schema_dir: Path, meta_schema: Path, output_dir: Path):
    loader = SchemaLoader(meta_schema)
    loader.load_all(schema_dir)

    converter = RedfishToToscaConverter()

    for schema in loader.schemas.values():
        resolved = dereference(schema, loader.registry)
        flattened = {**resolved, "properties": flatten_allOf(resolved)}
        node = converter.convert(flattened)
        ToscaWriter.write(node, output_dir)