Convert JSON Schema files to TOSCA Profiles
===

``json2tosca`` converts JSON Schema files to one or more TOSCA
Profiles. 

## Installation Instructions

### Setting up the virtual environment

``json2tosca`` is written in Python3.  We recommend that you run the
``json2tosca`` software in its own virtual environment. Create and
activate your virtual environment as follows:

    python3 -m venv env
    source env/bin/activate
    
``json2tosca`` uses
[PEP-517](https://www.python.org/dev/peps/pep-0517/) and
[PEP-518](https://www.python.org/dev/peps/pep-0518/) based
installation systems that require the latest version of ``pip``. To
upgrade ``pip`` to the latest version, run the following command in
your virtual environment:

    pip install -U pip 

### Installing the ``json2tosca`` software

The software can be installed directly from the git repository by
running the following command in your virtual environment:

    pip install git+https://github.com/lauwers/json2tosca
    
Alternatively, you can also clone the git repo first and then run the
installer in your local copy as follows:

    git clone https://github.com/lauwers/json2tosca
    cd json2tosca
    pip install . 
    
## Using ``json2tosca``

To convert an JSON Schema file, run the following command in your virtual environment:

    json2tosca --input <json schema file> --output <TOSCA Profile directory>
    
Note that ``json2tosca`` outputs profile *directories* rather than
profile *CSAR files*, since it is possible that a single JSON Schema
file can result in the creation of multiple TOSCA profiles.


