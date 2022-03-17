#
# Module for converting JSON Scheam files to TOSCA
#
__author__ = "Chris Lauwers"
__copyright__ = "Copyright (c) 2022, Ubicity Corp."
__email__ = "lauwers@ubicity.com"

# Logging support
import logging
logger = logging.getLogger(__name__)

class JSONSchema(object):
    """Class for converting JSON Schema data to TOSCA profiles.
    """

    def __init__(self, json_schema_data):
        """Constructor """

        # First, call superclass constructor
        super(JSONSchema, self).__init__()

        # Track the swagger data
        self.data = json_schema_data

        # Track types to avoid creating duplicates
        self.node_types = set()
        self.data_types = set()

        # Track schemas from which to create data types
        self.deferred_schemas = set()
        

    def convert(self, top):
        """Convert a Json Schema object to TOSCA type definitions and write
        these definitions to profile directories under 'top'.
        """

        # Create the directories within which each profile will be
        # created.
        self.initialize_profile(top)

        # Create data types based on 'deferred' schemas
        self.process_top_level_schema()

        # Clean up
        self.finalize_profiles()


    def initialize_profiles(self, top, info):
        for name, profile in self.profiles.items():
            profile.initialize(top, info)
        

    def finalize_profiles(self):
        for name, profile in self.profiles.items():
            profile.finalize()
        

    def create_node_type_from_schema_reference(self, name, reference):
        """Create a TOSCA node type from a JSON Schema reference"""

        # Get the referenced schema
        try:
            ref = reference['$ref']
        except KeyError:
            logger.error("%s: not a schema reference", name)
            return
        
        # Find the referenced schema:
        (schema_ref, node_type_schema) = self.get_referenced_schema(ref)
        if not node_type_schema:
            logger.info("%s: referenced schema not found", name)
            return
        
        # Create the node type for the referenced schema
        self.create_node_type_from_schema(schema_ref, node_type_schema)


    def create_node_type_from_schema(self, schema_name, schema):
        """Create a TOSCA node type from a JSON Schema. TODO: this should be
        moved into its own module.

        The basic JSON schema documentation can be found at:

        https://json-schema.org/

        The important documents are:

        JSON Schema Core: This document asserts what a JSON document
        must look like
          https://datatracker.ietf.org/doc/html/draft-wright-json-schema-01

        JSON Schema Validation. This document specifies a vocabulary
        for JSON Schema to describe the meaning of JSON documents:
          https://datatracker.ietf.org/doc/html/draft-wright-json-schema-validation-01

        Note that OpenAPI schema objects extend the standard JSON
        Schema objects. These extensions are documented in:
          https://swagger.io/specification/

        """
        
        # Avoid duplicates
        if schema_name in self.node_types:
            logger.info("%s: duplicate", schema_name)
            return
        self.node_types.add(schema_name)
        
        # If this schema is intended to define a node type, the schema
        # 'type' better be 'object'.
        try:
            if not schema['type'] == 'object':
                logger.error("Node type '%s' with type '%s'", schema_name, schema['type'])
                return
        except KeyError:
                logger.error("Trying to create node type '%s' without a type",
                             schema_name)
                return

        # Parse group, version, prefix, and kind from the schema name. 
        group, version, kind, prefix = self.parse_schema_name(schema_name, schema)
        # For now, we only handle v1
        if version and version != 'v1':
            logger.debug("Ignoring %s version of %s", version, kind)
            return

        # k8s 'resources' include a 'x-kubernetes-group-version-kind'
        # attribute. Note that the value of
        # 'x-kubernetes-group-version-kind' is a list. Not sure why.
        try:
            group_version_kind_list = schema['x-kubernetes-group-version-kind']
        except KeyError:
            logger.error("%s: creating node type without group version kind", schema_name)

        # Make sure we plan deferred creation of data types for any
        # properties defined in this schema
        self.create_data_types_for_properties(schema, defer=True)

        # Create the node type in the profile for this schema
        profile = self.profiles[group]
        profile.emit_node_type(kind, schema)
        

    def process_top_level_schema(self):
        """Process the top-level JSON Schema Objects
        """
        pass


    def create_data_type_from_schema(self, schema_name, schema):
        """Create a TOSCA data type from a JSON Schema"""

        # Avoid duplicates
        if schema_name in self.data_types:
            logger.debug("%s: duplicate", schema_name)
            return
        self.data_types.add(schema_name)

        # If this schema is intended to define a data type, this
        # schema must not reference another schema.
        try:
            ref = schema['$ref']
            logger.error("%s REFERENCES %s", schema_name, ref)
            return
        except KeyError:
            pass

        # Parse group, version, and kind from the schema name. 
        group, version, kind, prefix = self.parse_schema_name(schema_name, schema)
        logger.info("In profile %s", group)
        
        # We only handle v1 for now
        if version and version != "v1":
            logger.info("Ignoring %s version of %s", version, kind)
            return
        
        # k8s schemas for data types must not include a
        # 'x-kubernetes-group-version-kind' attribute. Note that the
        # value of 'x-kubernetes-group-version-kind' is a list. Not
        # sure why.
        try:
            group_version_kind_list = schema['x-kubernetes-group-version-kind']
            logger.error("%s: creating data type with group version kind", schema_name)
        except KeyError:
            pass
        
        # Make sure we define data types for any properties defined in
        # this schema
        try:
            self.create_data_types_for_properties(schema)
        except Exception as e:
            logger.error("%s: %s", schema_name, str(e))

        # Create the data type in the profile for this schema
        profile = self.profiles[group]
        profile.emit_data_type(kind, schema)


    def create_data_types_for_properties(self, schema, defer=False):
        """Create data types for property schemas referenced in this schema
        """
        # Does this schema have property definitions?
        try:
            properties = schema['properties']
        except KeyError:
            # No properties
            return

        # Do any properties reference schemas?
        for property_name, property_schema in properties.items():
            try:
                ref = property_schema['$ref']
                (schema_name, schema) = self.get_referenced_schema(ref)
                if defer:
                    self.deferred_schemas.add(schema_name)
                else:
                    self.create_data_type_from_schema(schema_name, schema)
            except KeyError:
                # Property schema does not contain a $ref. Items
                # perhaps?
                try:
                    items = property_schema['items']
                    ref = items['$ref']
                    (schema_name, schema) = self.get_referenced_schema(ref)
                    if defer:
                        self.deferred_schemas.add(schema_name)
                    else:
                        self.create_data_type_from_schema(schema_name, schema)
                except KeyError:
                    try:
                        additionalProperties = property_schema['additionalProperties']
                        ref = additionalProperties['$ref']
                        (schema_name, schema) = self.get_referenced_schema(ref)
                        if defer:
                            self.deferred_schemas.add(schema_name)
                        else:
                            self.create_data_type_from_schema(schema_name, schema)
                    except KeyError:
                        # No additional schemas
                        pass
                    pass


    def get_referenced_schema(self, ref):
        """Return the schema referenced by the 'ref' URL. This method returns
        a tuple consisting of the schema name and the schema
        """

        # Split into components
        split = ref.split('/')
        length = len(split)

        # Only support local references for now
        if split[0] != "#":
            logger.error("%s: not a local reference", ref)
            return None

        # Navigate to the referenced entity. 
        referenced = self.data
        i = 1
        while i < length:
            name = split[i]
            referenced = referenced[name]
            i = i+1

        # All done
        return (name, referenced)


    def parse_schema_name(self, schema_name, schema):
        """Parse schema name into a type info tuple that consists of
        'profile', 'version', 'name', and 'prefix'.
        """

        # Try to extract the model name from the schema.
        try:
            model_name = schema['x-swagger-router-model']
        except KeyError:
            # No router model. Just use schema name
            model_name = schema_name

        # Split schema name using '.' separator
        split = model_name.split('.')
        length = len(split)

        # If there is only one part, just return the model name as the
        # type name.
        if length < 2:
            return "", "", model_name, ""

        # If there are two parts, split the model name into a profile
        # name and a type, and use the model name as the prefix.
        if length == 2:
            return split[0], "", split[1], split[0]

        # Check if there is a version string. Versions start with 'v1'
        # or 'v2'
        if split[length-2][:2] == 'v1' or split[length-2][:2] == 'v2':
            version = split[length-2]
            prefix = split[length-3]
            profile = ".".join(split[0:length-2])
        else:
            version = ""
            prefix = split[length-2]
            profile = ".".join(split[0:length-1])
        name = split[length-1]
        return (profile, version, name, prefix)


