"""Schemas cerrados y metadatos del host para el proveedor MCP de escritura simulado."""
from datetime import datetime
import re
from .contracts import ContractError
from .proposals import uuid_text

UUID_PATTERN=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
HASH_PATTERN=r'^[0-9a-f]{64}$'

def object_schema(properties):
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}

BINDING={'operation_id':{'type':'string','pattern':UUID_PATTERN},'payload_hash':{'type':'string','pattern':HASH_PATTERN}}
SCHEMAS={
    'jenkins_current_revision':object_schema({}),
    'jenkins_find_request':object_schema(BINDING),
    'jenkins_close_request':object_schema(BINDING),
    'jenkins_request_tests':object_schema({**BINDING,
        'arguments':object_schema({'job':{'type':'string','enum':['atlas-tests']},
            'repository':{'type':'string','enum':['demo/atlas']},'revision':{'type':'string','enum':['revision-A','revision-B']}}),
        'not_after':{'type':'string','format':'date-time','maxLength':64}}),
}


def validate(name,values):
    if name not in SCHEMAS:raise ContractError('UNKNOWN_TOOL')
    if type(values) is not dict or set(values)!=set(SCHEMAS[name]['properties']):
        raise ContractError('INVALID_ARGUMENT')
    if values:
        identifier=values.get('operation_id');payload_hash=values.get('payload_hash')
        if (type(identifier) is not str or not re.fullmatch(UUID_PATTERN,identifier)
                or uuid_text(identifier)!=identifier or type(payload_hash) is not str
                or not re.fullmatch(HASH_PATTERN,payload_hash)):
            raise ContractError('INVALID_ARGUMENT')
    if name=='jenkins_request_tests':
        args=values['arguments']
        if (type(args) is not dict or set(args)!={'job','repository','revision'}
                or args['job']!='atlas-tests' or args['repository']!='demo/atlas'
                or args['revision'] not in ('revision-A','revision-B')):
            raise ContractError('INVALID_ARGUMENT')
        raw=values['not_after']
        try:
            if type(raw) is not str or len(raw)>64:raise ValueError()
            cutoff=datetime.fromisoformat(raw)
            if cutoff.utcoffset() is None:raise ValueError()
        except (ValueError,TypeError):raise ContractError('INVALID_ARGUMENT') from None
    return values


def validate_closure(data,operation_id,payload_hash):
    if (type(data) is not dict or set(data)!={'operation_id','payload_hash','receipt','closed','closed_at','evidence_id'}
            or data['operation_id']!=operation_id or data['payload_hash']!=payload_hash or data['closed'] is not True
            or type(data['evidence_id']) is not str or not re.fullmatch(r'closure:[0-9a-f]{64}',data['evidence_id'])
            or data['receipt'] not in (None,'queue:'+operation_id)):
        raise ContractError('INVALID_TOOL_OUTPUT')
    try:
        if type(data['closed_at']) is not str or len(data['closed_at'])>64 or datetime.fromisoformat(data['closed_at']).utcoffset() is None:raise ValueError()
    except (ValueError,TypeError):raise ContractError('INVALID_TOOL_OUTPUT') from None
    return data
