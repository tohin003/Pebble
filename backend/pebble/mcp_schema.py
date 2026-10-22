"""Bounded local JSON Schema; validation never retrieves remote references."""
import base64
import json
import re
from .policy import PolicyError

DIALECTS={'https://json-schema.org/draft/2020-12/schema','http://json-schema.org/draft-07/schema#','https://json-schema.org/draft-07/schema#'}


def inspect(schema):
    if not isinstance(schema,(dict,bool)) or len(json.dumps(schema,allow_nan=False).encode())>20000:raise PolicyError('MCP schema exceeded its object/boolean or byte boundary.')
    count=[0]
    def walk(value,depth=0):
        count[0]+=1
        if count[0]>1000 or depth>20:raise PolicyError('MCP schema complexity exceeded its review limit.')
        if isinstance(value,dict):
            if '$schema' in value and value['$schema'] not in DIALECTS:raise PolicyError('Unsupported JSON Schema dialect.')
            if '$id' in value:raise PolicyError('Schema identifiers that change reference scope are unsupported.')
            for key in ('$ref','$dynamicRef'):
                if key in value and (not isinstance(value[key],str) or not value[key].startswith('#') or len(value[key])>500):raise PolicyError('Only bounded local schema references are supported.')
            for key in ('allOf','anyOf','oneOf'):
                if isinstance(value.get(key),list) and len(value[key])>8:raise PolicyError('MCP schema has too many alternatives.')
            patterns=[value['pattern']] if 'pattern' in value else []
            if 'patternProperties' in value:
                if not isinstance(value['patternProperties'],dict) or len(value['patternProperties'])>16:raise PolicyError('Too many pattern properties.')
                patterns+=list(value['patternProperties'])
            if any(not isinstance(p,str) or len(p)>512 for p in patterns):raise PolicyError('Schema regular expression exceeded its limit.')
            for child in value.values():walk(child,depth+1)
        elif isinstance(value,list):
            for child in value:walk(child,depth+1)
    walk(schema)
    if isinstance(schema,dict) and schema.get('$ref')=='#':raise PolicyError('A directly self-referencing schema is unsupported.')
    return True


def worker(schema,arguments=None,check=False):
    from jsonschema import Draft202012Validator,Draft7Validator
    from referencing import Registry
    from referencing.exceptions import NoSuchResource
    inspect(schema)
    def deny(uri):raise NoSuchResource(ref=uri)
    validator=Draft7Validator if isinstance(schema,dict) and 'draft-07' in schema.get('$schema','') else Draft202012Validator
    validator.check_schema(schema)
    def patterns(value):
        if isinstance(value,dict):
            if 'pattern' in value:re.compile(value['pattern'])
            for p in value.get('patternProperties',{}):re.compile(p)
            for v in value.values():patterns(v)
        elif isinstance(value,list):
            for v in value:patterns(v)
    patterns(schema)
    return True if check else validator(schema,registry=Registry(retrieve=deny)).is_valid(arguments)


def header_value(value):
    raw=str(value).lower() if type(value) is bool else str(value)
    if not raw or raw!=raw.strip() or any(not 32<=ord(c)<=126 for c in raw) or raw.startswith('=?base64?') and raw.endswith('?='):
        return '=?base64?'+base64.b64encode(raw.encode()).decode()+'?='
    return raw


def header_routes(schema):
    paths={};seen=set();annotated=[]
    def scan(value):
        if isinstance(value,dict):
            if 'x-mcp-header' in value:annotated.append(id(value))
            for v in value.values():scan(v)
        elif isinstance(value,list):
            for v in value:scan(v)
    def props(node,path=()):
        if not isinstance(node,dict):return
        for key,value in node.get('properties',{}).items():
            if not isinstance(value,dict):continue
            if 'x-mcp-header' in value:
                name=value['x-mcp-header']
                if not isinstance(name,str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,80}",name) or name.casefold() in seen or value.get('type') not in {'string','integer','boolean'}:raise PolicyError('Invalid or duplicate MCP parameter-header annotation.')
                seen.add(name.casefold());paths[name]=path+(key,)
            props(value,path+(key,))
    scan(schema);props(schema)
    valid=[]
    for path in paths.values():
        node=schema
        for key in path:node=node['properties'][key]
        valid.append(id(node))
    if set(annotated)!=set(valid):raise PolicyError('MCP parameter headers require a static properties path.')
    return paths


def parameter_headers(schema,arguments):
    result={}
    for name,path in header_routes(schema).items():
        value=arguments
        for key in path:
            if not isinstance(value,dict) or key not in value:value=None;break
            value=value[key]
        if value is None:continue
        if type(value) not in {str,int,bool} or type(value) is int and abs(value)>2**53-1:raise PolicyError('MCP header parameter must be a safe primitive.')
        result['Mcp-Param-'+name]=header_value(value)
    return result
