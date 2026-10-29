"""Validated local emotion presets shared by the editor, cache keys and TTS worker."""
PRESETS={'inherit':'沿用参考音','happy':'开心','angry':'愤怒','sad':'悲伤','afraid':'害怕','disgusted':'厌恶','melancholic':'低落','surprised':'惊讶','calm':'平静'}
STRENGTHS={'low':.35,'medium':.6,'high':.8}
ORDER=('happy','angry','sad','afraid','disgusted','melancholic','surprised','calm')

def emotion_fields(row):
    name=row.get('emotion','inherit');strength=row.get('emotion_strength','medium')
    if not isinstance(name,str) or name not in PRESETS:raise ValueError('无效情绪预设')
    if not isinstance(strength,str) or strength not in STRENGTHS:raise ValueError('情绪强度只能为 low、medium 或 high')
    return {} if name=='inherit' else dict(emotion=name,emotion_strength=strength)

def emotion_key(row):
    value=emotion_fields(row)
    return (value.get('emotion','inherit'),value.get('emotion_strength','medium'))

def emotion_vector(row):
    value=emotion_fields(row)
    if not value:return None
    return [STRENGTHS[value['emotion_strength']] if name==value['emotion'] else 0.0 for name in ORDER]

def emotion_kwargs(row):
    vector=emotion_vector(row)
    return dict(emo_vector=vector,use_random=False) if vector else {}
