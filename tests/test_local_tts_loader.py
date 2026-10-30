import sys
from types import ModuleType
import pytest
from scripts.tts_local_models import local_semantic_loader


def test_owned_weight_loader_is_local_and_restores_class_method(tmp_path,monkeypatch):
    calls=[];weights=tmp_path/'model.safetensors';weights.write_bytes(b'owned tensors')
    class Config:
        @classmethod
        def from_pretrained(cls,path,**kwargs):
            assert path==tmp_path and kwargs=={'local_files_only':True}
            return 'config'
    class Model:
        @classmethod
        def from_pretrained(cls,path,*args,**kwargs):calls.append((path,kwargs));return 'model'
    original=Model.__dict__['from_pretrained']
    transformers=ModuleType('transformers');transformers.Wav2Vec2BertConfig=Config;transformers.Wav2Vec2BertModel=Model
    tensor_module=ModuleType('safetensors.torch');tensor_module.load=lambda data:{'weight':data}
    monkeypatch.setitem(sys.modules,'transformers',transformers);monkeypatch.setitem(sys.modules,'safetensors.torch',tensor_module)
    with local_semantic_loader(enabled=True):
        assert Model.from_pretrained(tmp_path,local_files_only=True)=='model'
        assert calls[-1]==(None,{'local_files_only':True,'config':'config','state_dict':{'weight':b'owned tensors'}})
        Model.from_pretrained(tmp_path/'absent',local_files_only=True)
        assert calls[-1][0]==tmp_path/'absent'
    assert Model.__dict__['from_pretrained'] is original
    with pytest.raises(RuntimeError):
        with local_semantic_loader(enabled=True):raise RuntimeError('constructor failed')
    assert Model.__dict__['from_pretrained'] is original
