#
# Module for reading JSON Schema file
#
__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022, Ubicity Corp."
__email__ = "lauwers@ubicity.com"

# Logging support
import logging
logger = logging.getLogger(__name__)

# YAML support. We'll use a YAML reader to read JSON
from ruamel.yaml import YAML

# System support
import sys

def read_json_schema(json_schema_file_name):
    """Parse json schema file content (in JSON) using the YAML parser

    Args:
      json_schema_file_name(str): name of the json_schema file

    Returns:
      dict containing parsed YAML data

    Raises:
      IOError: when unable to read file
    """

    # Open file
    json_schema_file = open(json_schema_file_name)

    # Read JSON data from the json_schema file
    json_schema_file_data = json_schema_file.read()

    # Return parsed file content as YAML. We use the RoundTripLoader
    # (the default) since it stores line numbers in the comments
    # section.
    yaml = YAML()
    return yaml.load(json_schema_file_data)
