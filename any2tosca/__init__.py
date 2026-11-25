"""
any2tosca
----------
Convert JSON Schema documents to TOSCA data types.

Public API::

    from any2tosca import convert_schema, build_tosca_document
    from any2tosca.yaml_utils import dump_tosca_yaml
"""

__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022-2026, Ubicity Corp."
__email__ = "info@ubicity.com"

from .converter import Converter, ConversionResult, Conventions, build_tosca_document, convert_schema
from .openapi import OpenAPIExtractor
from .redfish import RedfishNormalizer
from .yaml_utils import dump_tosca_yaml

__all__ = [
    "Converter", "ConversionResult", "Conventions", "convert_schema", "build_tosca_document",
    "OpenAPIExtractor", "RedfishNormalizer",
    "dump_tosca_yaml",
]
__version__ = "0.1.0"
