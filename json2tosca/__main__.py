#
# Command-Line tool for converting a JSON Schema file to one or more
# TOSCA profiles
#
__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022, Ubicity Corp."
__email__ = "lauwers@ubicity.com"

# Logging support
import logging
logger = logging.getLogger(__name__)

# Parse command line arguments
import argparse

# Version string
from . import __version__

# Support for swagger files
import json2tosca.read_json_schema as rj
import json2tosca.json_schema as js


def main():
    """Invoke JSON Schema to TOSCA converter"""

    # Create top-level command line argument parser
    parser = argparse.ArgumentParser(description='Convert JSON Schema file to TOSCA')
    parser.add_argument('-d', '--debug', action='store_true', default=False,
                        help='turn on debug logging')

    parser.add_argument('-i', '--input', action='store', help='JSON Schema input file')
    parser.add_argument('-o', '--output', action='store', help='tosca output directory')
    parser.add_argument('-v', '--version', action='version', version='%(prog)s ' + str(__version__))

    # Parse command line
    args = parser.parse_args()

    # Initialize logging
    if args.debug is True:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    # Get input file and output directory
    if args.input:
        logger.debug("Input file: %s", args.input)
    else:
        logger.error("No input file specified")
        exit(1)
    if args.output:
        logger.debug("TOSCA output directory: %s", args.output)

    # Read json_schema input
    try:
        json_schema_data = rj.read_json_schema(args.input)
    except Exception as e:
        logger.error("Error reading '%s': %s", args.input, str(e))
        exit(1)

    # Invoke the converter
    json_schema = js.JSONSchema(json_schema_data)

    # Convert
    try:
        json_schema.convert(args.output)
    except Exception as e:
        logger.error("Error converting '%s': %s", args.input, str(e))
        exit(4)


# Main
if __name__ == "__main__":
    main()
