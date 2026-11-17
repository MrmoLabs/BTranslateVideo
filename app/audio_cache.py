"""Validate completed PCM clips before reuse; keep broken files for diagnosis."""
from pathlib import Path
import uuid
import wave


def reusable_voice_clip(path, on_invalid=None):
    path=Path(path)
    try:
        with wave.open(str(path),'rb') as stream:
            frames=stream.getnframes();rate=stream.getframerate()
            frame_size=stream.getnchannels()*stream.getsampwidth()
            if frames<=0 or rate<=0 or frame_size<=0:raise ValueError('empty audio')
            if frames*frame_size>path.stat().st_size:raise ValueError('truncated audio')
            stream.setpos(frames-1)
            if len(stream.readframes(1))!=frame_size:raise ValueError('truncated audio payload')
        return True
    except FileNotFoundError:return False
    except (EOFError,ValueError,wave.Error):
        # Permission/disk errors are not corruption: let those errors propagate.
        archived=path.with_name(path.name+'.invalid-'+uuid.uuid4().hex+'.bak')
        try:path.replace(archived)
        except FileNotFoundError:return False
        if on_invalid:on_invalid(path,archived)
        return False
