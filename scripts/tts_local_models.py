"""Keep Windows semantic-model loading off fragile memory-mapped storage slices."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def local_semantic_loader(enabled=None):
    if enabled is None:
        enabled=os.name=='nt'
    if not enabled:
        yield
        return
    from safetensors.torch import load
    from transformers import Wav2Vec2BertConfig,Wav2Vec2BertModel
    model_class=Wav2Vec2BertModel
    original=model_class.from_pretrained
    descriptor=model_class.__dict__.get('from_pretrained')

    def from_local(cls,source,*args,**kwargs):
        weights=Path(source)/'model.safetensors' if source is not None else None
        if not weights or not weights.is_file() or kwargs.get('state_dict') is not None:
            return original(source,*args,**kwargs)
        config=kwargs.pop('config',None)
        if config is None:
            config=Wav2Vec2BertConfig.from_pretrained(source,local_files_only=True)
        # Owned byte buffers avoid a Windows torch storage access violation during mmap slicing.
        state=load(weights.read_bytes())
        print('Loading local semantic model from owned tensors (Windows).',flush=True)
        return original(None,*args,config=config,state_dict=state,**kwargs)

    model_class.from_pretrained=classmethod(from_local)
    try:
        yield
    finally:
        if descriptor is None:
            delattr(model_class,'from_pretrained')
        else:
            model_class.from_pretrained=descriptor
