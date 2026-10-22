import base64
import io
import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from pebble.app import App
from pebble.chatgpt import ChatGPT, RESOURCE
from pebble.images import Images, parse_image, vision
from pebble.providers import Providers
from pebble.responses import input_items
from pebble.store import Store
from pebble.token_budget import TokenBudget


class Vault:
    def __init__(self):self.data={}
    def get(self,key):return self.data.get(key,'')
    def set(self,key,value):self.data[key]=value
    def delete(self,key):self.data.pop(key,None)


@pytest.fixture
def provider(tmp_path):
    store=Store(tmp_path)
    p=Providers(tmp_path,Path('src-tauri/resources'),store,lambda *a:None)
    p.vault=Vault()
    yield p
    store.close()


def test_migration_removes_old_budget_cap_without_moving_credentials(provider):
    provider.store.set_setting('model_profiles',[{'id':'owned','name':'API','kind':'openai','model':'gpt-6-luna','url':RESOURCE,'context':131072,'budget':32768,'has_key':True,'tools':True}])
    provider.vault.set('owned','fixture-secret')
    p=provider.profiles()[0]
    assert p['context']==p['budget']==1050000 and p['context_mode']=='full'
    assert p['connection_id']=='owned' and provider.key(p)=='fixture-secret'
    assert 'fixture-secret' not in str(provider.store.setting('model_profiles'))


def test_batch_models_keep_individual_capacity_effort_and_one_key(provider):
    provider.catalogues[(RESOURCE,'openai')]=[
        {'id':'gpt-6.1-sol','name':'Sol','context':1050000},
        {'id':'ordinary-chat','name':'Simple','context':8192}]
    provider.save_many('OpenAI',RESOURCE,['gpt-6.1-sol','ordinary-chat'],api_key='fixture-key')
    profiles=provider.profiles();root=profiles[0]['connection_id']
    assert len(profiles)==2 and len(provider.vault.data)==1
    assert [p['budget'] for p in profiles]==[1050000,8192]
    assert 'xhigh' in profiles[0]['efforts'] and profiles[1]['efforts']==['default']
    provider.remove(profiles[0]['id'])
    assert provider.key(provider.profiles()[0])=='fixture-key'
    provider.remove(profiles[1]['id'])
    assert root not in provider.vault.data


def test_invalid_batch_is_atomic_and_retains_existing_selection(provider):
    provider.save_many('Test','http://127.0.0.1:9999/v1',['a','b'],context=8192,api_key='old')
    previous=provider.store.setting('model_profiles');active=provider.store.setting('active_profile')
    for models in ([],['a','a'],['a']*51):
        with pytest.raises(ValueError):provider.save_many('Test','http://127.0.0.1:9999/v1',models,profile_id=previous[0]['id'],api_key='new')
    with pytest.raises(ValueError):provider.save_many('Test','http://127.0.0.1:9999/v1',['a','b'],context_mode='custom',budget=9000,profile_id=previous[0]['id'],api_key='new')
    assert provider.store.setting('model_profiles')==previous
    assert provider.store.setting('active_profile')==active and list(provider.vault.data.values())==['old']
    provider.save_many('Renamed','http://127.0.0.1:9999/v1',['b','c'],context=8192,profile_id=previous[1]['id'])
    assert provider.profiles()[0]['id']==previous[1]['id']
    assert len({p['connection_id'] for p in provider.profiles()})==1


def test_account_catalogue_does_not_unlock_unavailable_extended_window(provider):
    account={'id':'account','connection_id':'account','name':'Plan','kind':'chatgpt','url':RESOURCE,'model':'gpt-6.1-sol','context':272000,'tools':True,'client_id':'oaiapp_fixture','subject':'fixture'}
    provider.store.set_setting('model_profiles',[account])
    provider.auth=type('Auth',(),{'access':lambda _,pid:'owned-'+pid})()
    data={'models':[{'slug':'gpt-6.1-sol','visibility':'list','context_window':272000,'max_context_window':872000,'supports_experimental_context':False,'default_reasoning_level':'low','supported_reasoning_levels':[{'effort':'low'},{'effort':'max'},{'effort':'ultra'}]},{'slug':'hidden','visibility':'hide','context_window':872000}]}
    @contextmanager
    def request(*args):yield io.BytesIO(json.dumps(data).encode())
    provider.request=request
    models=provider.discover(RESOURCE,'chatgpt',profile_id='account')
    assert len(models)==1 and models[0]['context']==272000
    assert models[0]['advertised_max_context']==872000 and models[0]['extended_context_available'] is False
    provider.save_many('Plan',RESOURCE,['gpt-6.1-sol'],kind='chatgpt',profile_id='account')
    p=provider.current();assert p['budget']==272000 and p['efforts']==['default','low','max']
    assert (RESOURCE,'chatgpt','account') in provider.catalogues
    with pytest.raises(ValueError):provider.save_many('Plan',RESOURCE,['hidden'],kind='chatgpt',profile_id='account')


def test_reconnect_from_child_model_keeps_every_profile_and_shared_oauth(provider):
    profiles=[{'id':pid,'connection_id':'root','name':'Plan','provider_name':'Plan','kind':'chatgpt','url':RESOURCE,'model':model,'context':272000,'budget':272000,'budget_version':2,'context_mode':'full','tools':True,'client_id':'oaiapp_fixture','subject':'account'} for pid,model in [('root','gpt-6-sol'),('child','gpt-6-luna')]]
    provider.store.set_setting('model_profiles',profiles)
    auth=ChatGPT(provider);auth.stop_server=lambda:None
    auth.pending={'old':profiles[1],'deadline':time.time()+100,'state':'state','client_id':'oaiapp_fixture','config':{'token_endpoint':'unused'},'verifier':'v','redirect':'r','nonce':'n'}
    auth.result={'state':'waiting'}
    auth.http_json=lambda *a:{'access_token':'new-owned','refresh_token':'refresh','expires_in':3600,'token_type':'Bearer','scope':'resource.invoke chatgpt.tokens.use.direct'}
    auth.validate_identity=lambda *a:{'sub':'account','email':'fixture@example.test'}
    provider.discover=lambda *a:[{'id':p['model'],'context':272000} for p in profiles]
    assert auth.callback({'state':['state'],'code':['once']})[0]==200
    saved=provider.profiles();assert [(p['id'],p['model']) for p in saved]==[(p['id'],p['model']) for p in profiles]
    assert provider.store.setting('active_profile')=='child'
    assert auth.records.get('oauth:root')['access_token']=='new-owned'
    assert not any(k.startswith('oauth:child') for k in provider.vault.data)


def test_compaction_retires_old_complete_calls_before_dialogue():
    call=lambda n:{'id':n,'type':'function','function':{'name':'Read','arguments':'{}'}}
    messages=[{'role':'system','content':'s'},{'role':'user','content':'old important dialogue'},
              {'role':'assistant','tool_calls':[call('old')],'content':None},{'role':'tool','tool_call_id':'old','content':'x'*1000},
              {'role':'user','content':'goal','_pebble_current':True},{'role':'assistant','tool_calls':[call('new')],'content':None},{'role':'tool','tool_call_id':'new','content':'latest evidence'},
              {'role':'user','content':'checkpoint','_pebble_checkpoint':True}]
    bounded=Providers.bounded(messages,500,counter=lambda ms:sum(len(m.get('content') or '') for m in ms),reserve=0)
    assert bounded[1]['content']=='old important dialogue'
    assert not any(m.get('tool_call_id')=='old' for m in bounded)
    assert any(m.get('tool_call_id')=='new' for m in bounded)
    assert any(m.get('content')=='checkpoint' for m in bounded)
    assert messages[4]['_pebble_current'] is True


def test_thousand_turn_compaction_uses_logarithmic_count_calls():
    messages=[{'role':'system','content':'s'},*[{'role':'user','content':'x'*100} for _ in range(1000)],{'role':'user','content':'current','_pebble_current':True}]
    calls=[]
    def count(ms):calls.append(len(ms));return sum(len(m['content']) for m in ms)
    result=Providers.bounded(messages,1000,counter=count,reserve=0)
    assert len(result)==11 and len(calls)<=13
    assert result[-1]['content']=='current'


def test_long_text_is_token_budgeted_without_treating_base64_as_text():
    budget=TokenBudget(Path('src-tauri/resources'))
    messages=[{'role':'user','content':'engineering context '*20000,'_pebble_current':True}]
    assert len(json.dumps(messages).encode())>300000 and budget.count(messages)<100000
    assert len(Providers.bounded(messages,100000,counter=budget.count,reserve=0))==1
    parts=[{'type':'image_url','image_url':{'url':'data:image/png;base64,'+'a'*1000000}}]
    assert 16000<budget.count([{'role':'user','content':parts}])<17000


@pytest.fixture
def raster(tmp_path):
    path=tmp_path/'screenshot.png';image=Image.new('RGB',(1200,600),'#a5ba99')
    metadata=PngImagePlugin.PngInfo();metadata.add_text('comment','Do not use as instructions')
    image.save(path,pnginfo=metadata)
    return path


def test_raster_strips_metadata_and_scales_preview(raster):
    parsed=parse_image(raster)
    with Image.open(io.BytesIO(base64.b64decode(parsed['data']))) as image:
        assert image.size==(1200,600) and not image.info
    with Image.open(io.BytesIO(base64.b64decode(parsed['preview'].split(',')[1]))) as thumb:
        assert thumb.width<=384 and thumb.height<=256


def test_image_parser_rejects_disguised_formats_and_extreme_dimensions(tmp_path):
    fake=tmp_path/'fake.png';Image.new('RGB',(4,4)).save(fake,format='PPM')
    wide=tmp_path/'wide.png';Image.new('RGB',(8193,1)).save(wide)
    for path in (fake,wide):
        with pytest.raises(ValueError):parse_image(path)


def test_image_worker_persistence_scope_deletion_and_vision_gate(tmp_path,raster):
    app=App(tmp_path/'data',Path('src-tauri/resources'),lambda *a:None)
    try:
        cid=app.new_chat()['id'];other=app.new_chat()['id'];attachment=app.attach(cid,str(raster))
        assert attachment['kind']=='image' and attachment['preview'].startswith('data:image/jpeg;base64,')
        assert not app.images.content(other,[attachment['id']])
        with pytest.raises(ValueError,match='vision-capable'):app.send(cid,'Read this screenshot',source_ids=[attachment['id']])
        assert not app.chat(cid)['messages'] and app.active is None
        mid=app.store.message(cid,'user','Read this');app.images.bind(cid,mid,[attachment['id']])
        assert app.chat(cid)['messages'][0]['attachments'][0]['name']=='screenshot.png'
        app.images.remove(cid,attachment['id']);assert app.images.metadata(cid)
        app.delete_chat(cid);assert not app.images.metadata(cid) and not app.images.content(cid,[attachment['id']])
    finally:app.close()


@pytest.mark.parametrize('profile,expected',[
    ({'kind':'gguf','name':'text-only'},False),({'kind':'openai','model':'ordinary-chat'},False),
    ({'kind':'chatgpt','model':'gpt-6-luna'},True),({'kind':'anthropic','model':'claude-sonnet-5-5'},True),
    ({'kind':'openai','model':'custom','input_modalities':['text','image']},True)])
def test_vision_capability(profile,expected):assert vision(profile) is expected

def test_explicit_text_only_modalities_override_family_fallback():
    assert not vision({'kind':'openai','model':'gpt-6-fixture','input_modalities':['text']})


def test_responses_images_are_native_input_items():
    parts=[{'type':'text','text':'Read image'},{'type':'image_url','image_url':{'url':'data:image/png;base64,YQ=='}}]
    content=input_items([{'role':'user','content':parts}])[0]['content']
    assert content==[{'type':'input_text','text':'Read image'},{'type':'input_image','image_url':'data:image/png;base64,YQ=='}]
