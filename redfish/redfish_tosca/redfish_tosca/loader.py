import json
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

class SchemaLoader:
    def __init__(self, meta_schema_path: Path):
        self.meta_schema = json.loads(meta_schema_path.read_text())
        self.registry = Registry()
        self.schemas = {}

    def load_all(self, schema_dir: Path):
        for file in schema_dir.rglob("*.json"):
            self.load(file)

    def load(self, file_path: Path):
        schema = json.loads(file_path.read_text())
        self._validate(schema)

        uri = schema.get("$id") or schema.get("id") or file_path.resolve().as_uri()
        resource = Resource.from_contents(schema)

        self.registry = self.registry.with_resource(uri, resource)
        self.schemas[uri] = schema

    def _validate(self, schema: dict):
        validator_cls = validators.validator_for(self.meta_schema)
        validator = validator_cls(self.meta_schema)
        errors = list(validator.iter_errors(schema))
        if errors:
            raise ValueError(errors[0].message)